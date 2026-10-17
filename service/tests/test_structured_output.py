"""Opt-in strict JSON-schema output (LLM_STRUCTURED_OUTPUT=1)."""
import types

import pytest

from app.agent import cross_table_reasoning as ctr
from app.agent import output_schemas as os_
from app.agent import replay_cache, semantic_reasoning, value_ai_review, verifier
from app.agent.llm_clients import GeminiClient, GroqClient
from app.rules import compiler




@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("LLM_STRUCTURED_OUTPUT", "1")


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("LLM_STRUCTURED_OUTPUT", raising=False)
    assert os_.schema_name_for(value_ai_review._SYSTEM_PROMPT) is None


def test_every_real_prompt_maps_to_its_schema(on):
    assert os_.schema_name_for(value_ai_review._SYSTEM_PROMPT) == "value_anomalies"
    assert os_.schema_name_for(semantic_reasoning._SYSTEM_PROMPT) == "row_flags"
    assert os_.schema_name_for(verifier._VERIFIER_SYSTEM_PROMPT) == "row_verifications"
    assert os_.schema_name_for(ctr._SYSTEM_PROMPT) == "pair_flags"
    assert os_.schema_name_for(ctr._VERIFIER_SYSTEM_PROMPT) == "pair_verifications"


def test_union_shaped_compiler_prompt_is_left_unconstrained(on):
    assert os_.schema_name_for(compiler._COMPILER_SYSTEM_PROMPT) is None


def _check_strict(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        for v in node.values():
            _check_strict(v)
    elif isinstance(node, list):
        for v in node:
            _check_strict(v)


@pytest.mark.parametrize("name", list(os_.SCHEMAS))
def test_schemas_obey_strict_mode_rules(name):
    _check_strict(os_.SCHEMAS[name])


class _Groq400(Exception):
    status_code = 400


def _groq(fail_structured=False):
    calls = []

    def create(**kw):
        calls.append(kw)
        if fail_structured and "response_format" in kw:
            raise _Groq400("json_validate_failed")
        msg = types.SimpleNamespace(content='{"anomalies": []}', reasoning=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")], usage=None)

    c = object.__new__(GroqClient)
    c._model = "openai/gpt-oss-20b"
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return c, calls


def test_groq_sends_strict_json_schema_when_on(on):
    c, calls = _groq()
    c.complete(value_ai_review._SYSTEM_PROMPT, "{}")
    rf = calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "value_anomalies"


def test_groq_sends_nothing_extra_when_off(monkeypatch):
    monkeypatch.delenv("LLM_STRUCTURED_OUTPUT", raising=False)
    c, calls = _groq()
    c.complete(value_ai_review._SYSTEM_PROMPT, "{}")
    assert "response_format" not in calls[0]


def test_groq_falls_back_to_plain_once_on_a_400_and_stops_retrying_structured(on):
    c, calls = _groq(fail_structured=True)
    c.complete(value_ai_review._SYSTEM_PROMPT, "{}")
    assert ["response_format" in k for k in calls] == [True, False]
    c.complete(value_ai_review._SYSTEM_PROMPT, "{}")
    assert "response_format" not in calls[-1] and len(calls) == 3


def test_groq_non_400_errors_are_not_swallowed(on):
    c, _ = _groq()

    def boom(**kw):
        raise RuntimeError("network")

    c._client.chat.completions.create = boom
    with pytest.raises(RuntimeError):
        c.complete(value_ai_review._SYSTEM_PROMPT, "{}")


def test_gemini_passes_schema_when_on_and_falls_back_on_400(on):
    seen = []

    class _E(Exception):
        code = 400

    def gen(model, contents, config):
        seen.append(config)
        if config.response_json_schema is not None and len(seen) == 1:
            raise _E("bad schema")
        return types.SimpleNamespace(text='{"anomalies": []}', usage_metadata=None)

    c = object.__new__(GeminiClient)
    c._model = "gemini-3.6-flash"
    c._client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))
    c.complete(value_ai_review._SYSTEM_PROMPT, "{}")
    assert seen[0].response_mime_type == "application/json" and seen[0].response_json_schema
    assert seen[1].response_json_schema is None and c._structured_rejected is True


def test_structured_and_plain_replies_use_different_cache_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    replay_cache.reset_stats_for_tests()
    c, calls = _groq()
    c._quota_scope = None
    c._token_bucket = None
    monkeypatch.delenv("LLM_STRUCTURED_OUTPUT", raising=False)
    c.complete_with_retry(value_ai_review._SYSTEM_PROMPT, "{}")
    monkeypatch.setenv("LLM_STRUCTURED_OUTPUT", "1")
    c.complete_with_retry(value_ai_review._SYSTEM_PROMPT, "{}")
    assert len(calls) == 2  # the second was a miss, not a replay of the plain reply
    c.complete_with_retry(value_ai_review._SYSTEM_PROMPT, "{}")
    assert len(calls) == 2  # now it is a hit

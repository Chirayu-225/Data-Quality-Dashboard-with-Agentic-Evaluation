"""Replay cache: identical prompt -> identical reply, no second provider call."""
from app.agent import replay_cache
from app.agent.llm_clients import LLMClient


class _CountingClient(LLMClient):
    _model = "fake-model-1"

    def __init__(self, replies):
        self.calls = 0
        self._replies = list(replies)

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return self._replies.pop(0)


def test_disabled_by_default_calls_provider_every_time(monkeypatch):
    monkeypatch.delenv("LLM_REPLAY_CACHE_DIR", raising=False)
    c = _CountingClient(["a", "b"])
    assert c.complete_with_retry("sys", "usr") == "a"
    assert c.complete_with_retry("sys", "usr") == "b"
    assert c.calls == 2


def test_second_identical_call_is_replayed(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    replay_cache.reset_stats_for_tests()
    c = _CountingClient(['{"flags": []}', "SHOULD NOT BE USED"])
    first = c.complete_with_retry("sys", "usr")
    second = c.complete_with_retry("sys", "usr")
    assert first == second == '{"flags": []}'
    assert c.calls == 1
    assert replay_cache.stats()["hits"] == 1


def test_different_prompt_or_model_is_a_different_key(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    c = _CountingClient(["one", "two", "three"])
    assert c.complete_with_retry("sys", "usr") == "one"
    assert c.complete_with_retry("sys", "other user prompt") == "two"
    c._model = "fake-model-2"
    assert c.complete_with_retry("sys", "usr") == "three"
    assert c.calls == 3


def test_prompts_are_never_stored_only_hashes(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    c = _CountingClient(["reply"])
    c.complete_with_retry("SECRET-SYSTEM", "SECRET-ROW-DATA Jane Doe")
    blob = "".join(p.read_text() for p in tmp_path.iterdir())
    assert "SECRET-ROW-DATA" not in blob and "SECRET-SYSTEM" not in blob


def test_empty_reply_is_not_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    c = _CountingClient(["", "real answer"])
    assert c.complete_with_retry("s", "u") == ""
    assert c.complete_with_retry("s", "u") == "real answer"
    assert c.calls == 2


def test_corrupt_cache_file_falls_back_to_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path))
    key = replay_cache.make_key("_CountingClient", "fake-model-1", "s", "u")
    (tmp_path / f"{key}.json").write_text("{not json")
    c = _CountingClient(["fresh"])
    assert c.complete_with_retry("s", "u") == "fresh"
    assert c.calls == 1


def test_key_has_no_field_boundary_ambiguity():
    assert replay_cache.make_key("a", "b", "cd", "e") != replay_cache.make_key("a", "b", "c", "de")

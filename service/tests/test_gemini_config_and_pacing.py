import time
import types

import pytest

from app.agent import llm_clients as lc
from app.config import gemini_model, gemini_requests_per_minute


def test_default_model_and_env_override(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini_model() == "gemini-3.5-flash-lite"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    assert gemini_model() == "gemini-3.1-flash-lite"


@pytest.mark.parametrize("raw,expected", [(None, 5), ("12", 12), ("0", None), ("off", None)])
def test_rpm_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("GEMINI_REQUESTS_PER_MINUTE", raising=False)
    else:
        monkeypatch.setenv("GEMINI_REQUESTS_PER_MINUTE", raw)
    assert gemini_requests_per_minute() == expected


def test_pacer_spaces_calls_and_first_call_is_free():
    p = lc._RequestPacer(600)  # 0.1 s apart
    t0 = time.monotonic()
    p.acquire()
    first = time.monotonic() - t0
    p.acquire()
    p.acquire()
    total = time.monotonic() - t0
    assert first < 0.05 and 0.18 <= total < 0.6


def test_pacer_is_shared_per_account_and_model():
    a = lc._shared_pacer("gemini", "key1", "m", 5)
    assert a is lc._shared_pacer("gemini", "key1", "m", 5)
    assert a is not lc._shared_pacer("gemini", "key1", "other-model", 5)
    assert a is not lc._shared_pacer("gemini", "key2", "m", 5)


def test_client_paces_requests_through_complete_with_retry(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_REQUESTS_PER_MINUTE", "600")
    monkeypatch.delenv("LLM_REPLAY_CACHE_DIR", raising=False)
    lc._reset_shared_state_for_tests()
    c = lc.GeminiClient()
    assert c._model == "gemini-3.5-flash-lite" and c._request_pacer.min_interval == pytest.approx(0.1)
    c.complete = lambda s, u: "{}"
    t0 = time.monotonic()
    for i in range(3):
        c.complete_with_retry("sys", f"user {i}")
    assert time.monotonic() - t0 >= 0.18


def test_pacing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_REQUESTS_PER_MINUTE", "off")
    assert lc.GeminiClient()._request_pacer is None

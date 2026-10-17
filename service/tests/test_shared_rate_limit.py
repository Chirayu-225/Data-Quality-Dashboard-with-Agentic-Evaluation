"""The TPM budget and daily-quota breaker are process-wide per
(provider, key): a client per request must NOT mean a budget per request."""

import threading

import pytest

from app.agent import llm_clients as lc
from app.agent.llm_clients import LLMClient, LLMUnavailableError


class _Stub(LLMClient):
    def __init__(self, key="k1", provider="groq", tpm=7000, fail=None):
        self._init_rate_limit(tpm, provider=provider, api_key=key)
        self.calls = 0
        self.fail = fail

    def complete(self, s, u):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        return "{}"


@pytest.fixture(autouse=True)
def _clean():
    lc._reset_shared_state_for_tests()
    yield
    lc._reset_shared_state_for_tests()


def test_two_clients_same_key_share_one_bucket():
    a, b = _Stub(), _Stub()
    assert a._token_bucket is b._token_bucket


def test_different_keys_or_providers_do_not_share():
    assert _Stub(key="k1")._token_bucket is not _Stub(key="k2")._token_bucket
    assert _Stub(provider="groq")._token_bucket is not _Stub(provider="gemini")._token_bucket


def test_budget_is_global_across_clients():
    a, b = _Stub(tpm=1000), _Stub(tpm=1000)
    a._token_bucket.acquire(600)
    assert b._token_bucket._tokens_used == 600


def test_bucket_is_thread_safe_under_contention():
    bucket = lc._shared_bucket("groq", "k", 10_000_000)
    def work():
        for _ in range(1000):
            bucket.acquire(1)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert bucket._tokens_used == 8000


def test_daily_quota_trips_a_breaker_every_client_sees():
    msg = "Rate limit on tokens per day (TPD): Limit 200000. Please try again in 33m2.448s."
    first = _Stub(fail=msg)
    with pytest.raises(LLMUnavailableError):
        first.complete_with_retry("s", "u")
    second = _Stub()  # a different job's fresh client
    with pytest.raises(LLMUnavailableError) as ei:
        second.complete_with_retry("s", "u")
    assert second.calls == 0
    assert "daily token quota" in str(ei.value)


def test_retry_hint_parsing():
    assert lc._parse_retry_seconds("try again in 33m2.448s") == pytest.approx(33 * 60 + 2.448)
    assert lc._parse_retry_seconds("try again in 1h2m") == 3720
    assert lc._parse_retry_seconds("no hint") == lc._DEFAULT_DAILY_BLOCK_SECONDS


# --- reserve-then-settle accounting ---


class _Usage(_Stub):
    def __init__(self, prompt, completion, **kw):
        super().__init__(**kw)
        self._u = (prompt, completion)

    def complete(self, s, u):
        lc._record_usage(self._u[0], self._u[1], sum(self._u))
        return "{}"


def test_bucket_is_corrected_to_actual_usage_after_the_call():
    c = _Usage(100, 50, tpm=100_000)
    c.complete_with_retry("s", "u")
    assert c._token_bucket._tokens_used == 150  # not prompt-est + default allowance


def test_short_replies_pull_the_expected_reservation_down():
    c = _Usage(100, 10, tpm=100_000)
    for _ in range(12):
        c.complete_with_retry("s", "u")
    ema = lc._OUTPUT_EMA[c._quota_scope]
    assert ema < lc._OUTPUT_TOKEN_ALLOWANCE * 0.2


def test_failed_calls_are_refunded_not_charged():
    c = _Stub(tpm=100_000, fail="boom")
    with pytest.raises(LLMUnavailableError):
        c.complete_with_retry("s", "u")
    assert c._token_bucket._tokens_used == 0


def test_usage_totals_accumulate_for_reconciliation():
    _Usage(100, 50).complete_with_retry("s", "u")
    _Usage(200, 20).complete_with_retry("s", "u")
    snap = list(lc.usage_snapshot().values())[0]
    assert snap == {"prompt": 300, "completion": 70, "total": 370, "calls": 2}


# --- cancellation reaches provider calls and pacing sleeps ---


def test_cancel_check_stops_a_call_before_it_reaches_the_provider():
    from app import cancellation

    c = _Stub()
    cancellation.set_check(lambda: True)
    try:
        with pytest.raises(cancellation.JobCancelled):
            c.complete_with_retry("s", "u")
    finally:
        cancellation.set_check(None)
    assert c.calls == 0


def test_cancel_interrupts_a_long_pacing_wait_quickly():
    import time as _t

    from app import cancellation

    c = _Stub(tpm=1000)
    c._token_bucket.acquire(900)  # next big call must wait for the window
    flag = {"stop": False}
    cancellation.set_check(lambda: flag["stop"])
    threading.Timer(0.3, lambda: flag.update(stop=True)).start()
    t0 = _t.monotonic()
    try:
        with pytest.raises(cancellation.JobCancelled):
            c._token_bucket.acquire(500)
    finally:
        cancellation.set_check(None)
    assert _t.monotonic() - t0 < 3  # not the ~60 s window


def test_cancel_is_not_swallowed_by_broad_except_exception():
    from app.cancellation import JobCancelled

    assert not issubclass(JobCancelled, Exception)


def test_usage_is_logged_and_totalled_even_without_a_bucket():
    class NoBucket(LLMClient):
        def __init__(self):
            self._init_rate_limit(None, provider="gemini", api_key="gk")

        def complete(self, s, u):
            lc._record_usage(10, 5, 15)
            return "{}"

    NoBucket().complete_with_retry("s", "u")
    snap = lc.usage_snapshot()
    assert any(k.startswith("gemini:") and v["total"] == 15 for k, v in snap.items())

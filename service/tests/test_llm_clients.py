from unittest.mock import MagicMock, patch

import pytest

from app.agent.llm_clients import (
    GeminiClient,
    GroqClient,
    LLMClient,
    LLMRequestTooLargeError,
    LLMUnavailableError,
    _deterministic_seed,
    _estimate_tokens,
    _TokenBucket,
)
from app.config import ConfigError


class _FakeStatusError(Exception):
    """Stands in for a real SDK exception (e.g. groq.APIStatusError)
    without depending on any specific SDK's class hierarchy —
    `_is_request_too_large` only cares about `.status_code`."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _CountingLLM(LLMClient):
    """Fails its first N calls, then succeeds — used to test
    `complete_with_retry`'s retry behavior without hitting a real
    provider. `time.sleep` is patched out in these tests so the real
    backoff delays don't slow the suite down."""

    def __init__(self, fail_times: int, error_cls: type[Exception] = RuntimeError):
        self.fail_times = fail_times
        self.error_cls = error_cls
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error_cls(f"transient failure #{self.calls}")
        return "success"


class _AlwaysFailsLLM(LLMClient):
    def __init__(self):
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("permanently down")


def test_groq_client_raises_clear_error_without_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        GroqClient()


def test_groq_client_complete_calls_sdk_correctly(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    with patch("groq.Groq") as mock_groq_cls:
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="hello from groq"))]
        mock_client.chat.completions.create.return_value = mock_response

        client = GroqClient()
        result = client.complete("system prompt", "user prompt")

        assert result == "hello from groq"
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"][0] == {"role": "system", "content": "system prompt"}
        assert call_kwargs["messages"][1] == {"role": "user", "content": "user prompt"}
        assert call_kwargs["temperature"] == 0
        # Item 6 (live determinism) — a content-derived seed goes on
        # every call, not left unset. See _deterministic_seed's own
        # docstring for why this is a mitigation, not a guarantee.
        assert call_kwargs["seed"] == _deterministic_seed("system prompt", "user prompt")


def test_gemini_client_raises_clear_error_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        GeminiClient()


def test_gemini_client_complete_calls_sdk_correctly(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with patch("google.genai.Client") as mock_genai_cls:
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.text = "hello from gemini"
        mock_client.models.generate_content.return_value = mock_response

        client = GeminiClient()
        result = client.complete("system prompt", "user prompt")

        assert result == "hello from gemini"
        mock_client.models.generate_content.assert_called_once()
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert call_kwargs["contents"] == "user prompt"
        config = call_kwargs["config"]
        assert config.temperature == 0
        assert config.seed == _deterministic_seed("system prompt", "user prompt")


def test_complete_with_retry_succeeds_after_transient_failures(monkeypatch):
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", lambda _: None)
    llm = _CountingLLM(fail_times=2)  # fails twice, succeeds on the 3rd (last) attempt

    result = llm.complete_with_retry("sys", "user")

    assert result == "success"
    assert llm.calls == 3


def test_complete_with_retry_raises_llm_unavailable_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", lambda _: None)
    llm = _AlwaysFailsLLM()

    with pytest.raises(LLMUnavailableError) as exc_info:
        llm.complete_with_retry("sys", "user")

    # Exactly 3 attempts (the documented policy), not more, not fewer —
    # and never a raw provider exception escaping to the caller.
    assert llm.calls == 3
    assert "permanently down" in str(exc_info.value)
    assert isinstance(exc_info.value.last_error, RuntimeError)


def test_complete_with_retry_backs_off_between_attempts(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", sleep_calls.append)
    llm = _AlwaysFailsLLM()

    with pytest.raises(LLMUnavailableError):
        llm.complete_with_retry("sys", "user")

    # 2 sleeps between 3 attempts, exponentially increasing (1s, 2s) —
    # never a sleep after the final attempt, since there's nothing left
    # to wait for at that point.
    assert sleep_calls == [1.0, 2.0]


def test_complete_with_retry_does_not_retry_on_first_success(monkeypatch):
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", lambda _: None)
    llm = _CountingLLM(fail_times=0)

    result = llm.complete_with_retry("sys", "user")

    assert result == "success"
    assert llm.calls == 1  # no wasted retries when the first attempt works


def test_complete_with_retry_fails_fast_on_413_without_retrying(monkeypatch):
    """A 413 (payload too large) is structurally unretryable — waiting
    or resubmitting the same prompt can never succeed, so this should
    NOT consume the normal 3-attempt retry budget or sleep at all."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", sleep_calls.append)

    class _TooLargeLLM(LLMClient):
        def __init__(self):
            self.calls = 0

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            raise _FakeStatusError("Request too large", status_code=413)

    llm = _TooLargeLLM()

    with pytest.raises(LLMRequestTooLargeError) as exc_info:
        llm.complete_with_retry("sys", "user")

    assert llm.calls == 1  # not retried — one failed attempt, not three
    assert sleep_calls == []  # no backoff wait either — waiting can't help
    assert isinstance(exc_info.value.last_error, _FakeStatusError)


def test_complete_with_retry_still_retries_429_and_503_normally(monkeypatch):
    """Only a 413 gets the fail-fast treatment — an ordinary rate-limit
    or server-busy error (no status_code, or a different one) still
    goes through the normal retry/backoff loop unchanged."""
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", lambda _: None)

    class _RateLimitedThenOkLLM(LLMClient):
        def __init__(self):
            self.calls = 0

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise _FakeStatusError("Rate limit reached", status_code=429)
            return "ok"

    llm = _RateLimitedThenOkLLM()
    result = llm.complete_with_retry("sys", "user")

    assert result == "ok"
    assert llm.calls == 2  # retried normally, unlike the 413 case above


# --- Live determinism mitigation (item 6) --------------------------------


def test_deterministic_seed_is_stable_for_the_same_exact_prompt():
    """The whole point: re-running the SAME prompt text must request
    the SAME seed from the provider every time — no call counter, no
    wall-clock component, nothing but the text itself."""
    seed_a = _deterministic_seed("system prompt", "user prompt")
    seed_b = _deterministic_seed("system prompt", "user prompt")
    assert seed_a == seed_b


def test_deterministic_seed_differs_for_different_prompts():
    seed_a = _deterministic_seed("system prompt", "user prompt A")
    seed_b = _deterministic_seed("system prompt", "user prompt B")
    assert seed_a != seed_b


def test_deterministic_seed_is_sensitive_to_the_system_prompt_too():
    """Not just the user prompt — a system-prompt change (e.g. a future
    prompt-hardening edit) must also change the seed, since the
    provider's determinism guarantee (such as it is) is scoped to the
    whole request, not just one half of it."""
    seed_a = _deterministic_seed("system A", "same user prompt")
    seed_b = _deterministic_seed("system B", "same user prompt")
    assert seed_a != seed_b


def test_deterministic_seed_is_a_non_negative_integer_in_a_safe_range():
    """Both providers document `seed` as an ordinary integer field —
    this just confirms the helper never hands back something a
    provider's own validation (negative, or outside a 32-bit-ish
    range) might reject outright."""
    seed = _deterministic_seed("x" * 500, "y" * 500)
    assert isinstance(seed, int)
    assert 0 <= seed <= 0x7FFFFFFF


def test_estimate_tokens_is_roughly_chars_over_four():
    assert _estimate_tokens("") == 1  # never zero — a call always costs something
    assert _estimate_tokens("a" * 400) == 100
    assert _estimate_tokens("a" * 401) == 101  # rounds up, never underestimates


class _FakeClock:
    """A controllable fake for time.monotonic()/time.sleep(), so
    _TokenBucket's minute-window behavior can be tested without a real
    test taking a minute (or several) to run. sleep() advances the
    clock by exactly the requested amount, same as real time passing."""

    def __init__(self):
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


def test_token_bucket_does_not_block_when_budget_available(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.agent.llm_clients.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", clock.sleep)

    bucket = _TokenBucket(tokens_per_minute=1000)
    bucket.acquire(200)
    bucket.acquire(300)

    assert clock.sleep_calls == []  # 500/1000 used — never needed to wait


def test_token_bucket_blocks_until_window_rolls_over_when_exceeded(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.agent.llm_clients.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", clock.sleep)

    bucket = _TokenBucket(tokens_per_minute=100)
    bucket.acquire(80)  # 80/100 used, fits fine
    bucket.acquire(30)  # 80+30=110 > 100 — must wait for the window to roll over

    assert clock.sleep_calls  # blocked at least once instead of firing anyway
    assert clock.now >= 60.0  # actually waited out a full window, not a token amount


def test_token_bucket_lets_an_oversized_single_call_through_when_window_empty(monkeypatch):
    """A call bigger than the entire budget must still be let through
    eventually (once the window is otherwise empty) — refusing it
    forever would mean the client could never complete that call at
    all, which is worse than letting the provider itself reject it."""
    clock = _FakeClock()
    monkeypatch.setattr("app.agent.llm_clients.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", clock.sleep)

    bucket = _TokenBucket(tokens_per_minute=100)
    bucket.acquire(500)  # way over budget, but window was empty — let through

    assert clock.sleep_calls == []  # no infinite loop, no unnecessary wait either


def test_groq_client_defaults_to_a_conservative_tpm_budget(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GROQ_TOKENS_PER_MINUTE", raising=False)
    with patch("groq.Groq"):
        client = GroqClient()
        assert client._token_bucket is not None
        assert client._token_bucket.tokens_per_minute == 7000


def test_groq_client_respects_env_override_for_tpm_budget(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_TOKENS_PER_MINUTE", "3000")
    with patch("groq.Groq"):
        client = GroqClient()
        assert client._token_bucket.tokens_per_minute == 3000


def test_gemini_client_has_no_pacing_by_default(monkeypatch):
    """No TPM limit has actually been observed against Gemini in this
    project — pacing stays off by default rather than guessing a
    number, unlike Groq where a real observed limit exists."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_TOKENS_PER_MINUTE", raising=False)
    with patch("google.genai.Client"):
        client = GeminiClient()
        assert client._token_bucket is None


def test_complete_with_retry_paces_calls_through_the_token_bucket(monkeypatch):
    """End-to-end: a client configured with a real TPM budget actually
    gets paced when used through complete_with_retry, not just when
    _TokenBucket is tested in isolation."""
    clock = _FakeClock()
    monkeypatch.setattr("app.agent.llm_clients.time.monotonic", clock.monotonic)
    monkeypatch.setattr("app.agent.llm_clients.time.sleep", clock.sleep)

    class _PacedLLM(LLMClient):
        def __init__(self):
            self._init_rate_limit(tokens_per_minute=10)  # tiny budget, easy to exceed
            self.calls = 0

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            self.calls += 1
            return "ok"

    llm = _PacedLLM()
    # Each call's prompt is long enough to roughly cost more than the
    # whole 10-token budget on its own, so the second call must wait
    # for a fresh window rather than firing immediately after the first.
    big_prompt = "x" * 40  # ~10 estimated tokens
    llm.complete_with_retry("sys", big_prompt)
    llm.complete_with_retry("sys", big_prompt)

    assert llm.calls == 2  # both eventually went through
    assert clock.sleep_calls  # but the second one had to wait, not fire instantly


def _groq_call_kwargs(monkeypatch, model=None, effort_env=None):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    if effort_env is None:
        monkeypatch.delenv("GROQ_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("GROQ_REASONING_EFFORT", effort_env)
    with patch("groq.Groq") as mock_groq_cls:
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client.chat.completions.create.return_value = mock_response
        client = GroqClient(model=model) if model else GroqClient()
        client.complete("s", "u")
        return mock_client.chat.completions.create.call_args.kwargs


def test_groq_requests_low_reasoning_effort_by_default_for_gpt_oss(monkeypatch):
    """Live finding: default effort let gpt-oss-20b spend its whole
    2,048-token output budget thinking and return an empty answer."""
    assert _groq_call_kwargs(monkeypatch)["reasoning_effort"] == "low"


def test_groq_reasoning_effort_can_be_overridden_or_turned_off(monkeypatch):
    assert _groq_call_kwargs(monkeypatch, effort_env="medium")["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in _groq_call_kwargs(monkeypatch, effort_env="default")


def test_groq_reasoning_effort_is_not_sent_to_other_model_families(monkeypatch):
    assert "reasoning_effort" not in _groq_call_kwargs(monkeypatch, model="llama-3.3-70b-versatile")


def test_groq_rejects_an_invalid_reasoning_effort_value(monkeypatch):
    from app.config import ConfigError

    with pytest.raises(ConfigError):
        _groq_call_kwargs(monkeypatch, effort_env="turbo")


def test_groq_empty_reply_logs_why_without_row_content(monkeypatch, caplog):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    with patch("groq.Groq") as mock_groq_cls:
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        choice = MagicMock(finish_reason="length", message=MagicMock(content="", reasoning="x" * 50))
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[choice], usage=MagicMock(prompt_tokens=5000, completion_tokens=2048, total_tokens=7048)
        )
        client = GroqClient()
        with caplog.at_level("WARNING"):
            with pytest.raises(LLMRequestTooLargeError):
                client.complete("s", "SECRET-ROW-VALUE")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "finish_reason=length" in text and "completion_tokens=2048" in text
    assert "SECRET-ROW-VALUE" not in text


def test_output_budget_exhaustion_is_not_retried_and_lets_the_caller_shrink(monkeypatch):
    """Live finding: even at low effort one 45-row batch spent all 2,048
    output tokens reasoning (finish_reason=length). Retrying the same
    prompt repeats it, so it must surface at once as a too-large request."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    with patch("groq.Groq") as mock_groq_cls:
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        choice = MagicMock(finish_reason="length", message=MagicMock(content="", reasoning="x"))
        mock_client.chat.completions.create.return_value = MagicMock(choices=[choice], usage=None)
        client = GroqClient()
        with pytest.raises(LLMRequestTooLargeError):
            client.complete_with_retry("s", "u")
        assert mock_client.chat.completions.create.call_count == 1


def test_semantic_reasoner_halves_the_batch_when_the_model_runs_out_of_output_budget():
    from app.agent.semantic_reasoning import SemanticReasoner

    class _Budget(LLMClient):
        def __init__(self):
            self.sizes = []

        def complete(self, system_prompt, user_prompt):
            n = user_prompt.count("[UNTRUSTED DATA] row ")
            self.sizes.append(n)
            if n > 12:
                raise LLMRequestTooLargeError("budget", RuntimeError("finish_reason=length"))
            return '{"flags": []}'

    from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType

    rows = [{"a": str(i)} for i in range(60)]
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="t",
        columns=[ColumnSchema(name="a", type=ColumnType.STRING)], rows=rows,
    )
    llm = _Budget()
    result = SemanticReasoner(llm, sample_size=45).run(table)
    assert llm.sizes[:3] == [45, 22, 11]
    assert result.rows_sampled == 11

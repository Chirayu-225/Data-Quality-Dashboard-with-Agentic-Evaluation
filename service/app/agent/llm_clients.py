"""
LLM client wrappers — same spirit as the Connector interface: callers
depend on `LLMClient`, never on Groq or Gemini specifics directly.
This is what makes the generator/verifier provider split (Groq for the
generator, Gemini for the verifier, per the locked design decision to
avoid correlated blind spots between the two) a config choice, not a
code fork.

SDK imports are deliberately deferred to inside each class's
__init__, not at module load time — so importing this module (e.g. for
type-checking or testing unrelated code) never requires either SDK
package or either API key to be present.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from abc import ABC, abstractmethod

from app.agent import output_schemas, replay_cache
from app.cancellation import raise_if_cancelled
from app.config import (
    gemini_api_key,
    gemini_model,
    gemini_requests_per_minute,
    gemini_tokens_per_minute,
    groq_api_key,
    groq_reasoning_effort,
    groq_tokens_per_minute,
)

# Retry policy, applied uniformly to every provider via
# `complete_with_retry` below rather than duplicated per client.
# Numbers per the explicit product decision: 3 attempts total,
# exponential backoff (1s -> 2s -> 4s between attempts), so total
# added wait before giving up is ~7s — comfortably under the ~10-15s
# budget a user will wait for one dashboard section, even accounting
# for the calls' own latency.
#
# This is deliberately a SEPARATE, secondary mechanism from the token
# bucket below, not a replacement for it. Retry/backoff reacts after a
# call fails; it was never going to prevent a self-inflicted rate-limit
# violation in the first place — see _TokenBucket's docstring for why.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0


def _is_request_too_large(exc: Exception) -> bool:
    """True for a provider error whose HTTP status is 413 (payload too
    large) — e.g. Groq's "Request too large ... tokens per minute"
    response when a single call's prompt exceeds its per-request token
    cap. Duck-typed on `status_code` rather than importing Groq's
    exception classes, so this works for any SDK's error object without
    a hard dependency, and safely returns False for exceptions (like
    Gemini's transient 503s, or Groq's own 429 daily-cap error) that
    either lack this attribute or carry a different status."""
    return getattr(exc, "status_code", None) == 413


def _deterministic_seed(system_prompt: str, user_prompt: str) -> int:
    """A seed derived from the EXACT prompt text being sent, not the
    wall clock or OS entropy — passed to the provider's own `seed`
    parameter (see GroqClient.complete/GeminiClient.complete below).

    This is a MITIGATION, not a guarantee of determinism — see each
    client's docstring and docs/DATA_HANDLING.md's sibling,
    docs/SCORING.md's "Live determinism" known limitation, for the
    actual, sourced evidence: both providers document `seed` as
    "best effort" only. Groq's own docs say so explicitly ("Determinism
    is not guaranteed... refer to system_fingerprint to monitor changes
    in the backend"); Gemini's own community forum has reports of
    different output for an identical prompt even with a fixed seed
    and temperature=0 on some models. What pinning a seed DOES give,
    even without a guarantee: the two calls most likely to actually be
    identical (same exact prompt text, submitted close together,
    before any backend change) now have the best chance either
    provider's own infrastructure offers — strictly better than
    leaving `seed` unset (equivalent to a provider-chosen random seed
    every call) at zero cost, even though it cannot promise bit-for-bit
    reproducibility the way this project's OWN sampling seeding
    (see semantic_reasoning.py's _content_seed) can.

    Keeping the seed a pure function of the prompt TEXT (not the
    table, not a call counter) means two independent calls that happen
    to carry the identical prompt — the real case this matters for,
    a re-scan of the same file — always request the same seed from the
    provider, with no shared state needed between them."""
    digest = hashlib.sha256((system_prompt + "\x00" + user_prompt).encode("utf-8", errors="replace"))
    # Both providers document `seed` as an ordinary (signed 32-bit-ish)
    # integer — masking to 31 bits keeps this comfortably inside
    # whatever range either actually accepts, without needing to pin
    # down each provider's exact limit.
    return int.from_bytes(digest.digest()[:4], "big") & 0x7FFFFFFF


def _estimate_tokens(text: str) -> int:
    """Rough, provider-agnostic token estimate — no tokenizer
    dependency, deliberately conservative (rounds up). ~4 characters
    per token is the standard rule-of-thumb approximation for English
    text used by most providers' own docs; it doesn't need to be exact
    to do its job here, only close enough that the bucket doesn't let
    a call through that will actually blow the real budget."""
    return max(1, (len(text) + 3) // 4)


_log = logging.getLogger(__name__)


class _TokenBucket:
    """A fixed-window (not a precise rolling log — simple and close
    enough for pacing) tokens-per-minute budget, shared by every call
    a client makes. This is the fix for the failure mode retry/backoff
    can't touch: firing a batch of calls back-to-back can push a full
    minute's token budget through in a few seconds, which a provider's
    TPM cap rejects outright — no number of retries recovers from that,
    because the retries hit the same still-exhausted window. `acquire`
    blocks (sleeps) BEFORE a call goes out if there isn't enough budget
    left in the current window, rather than firing and hoping.

    A single call larger than the entire per-minute budget is let
    through once the window is otherwise empty — refusing it outright
    would mean no client could ever complete a large single call, which
    is worse than the (unavoidable) risk of that one call being
    rejected by the provider on its own terms."""

    _WINDOW_SECONDS = 60.0

    def __init__(self, tokens_per_minute: int):
        self.tokens_per_minute = tokens_per_minute
        self._window_start = time.monotonic()
        self._tokens_used = 0
        # Shared across worker threads (see _shared_bucket), so every
        # read-modify-write of the window happens under this lock.
        self._lock = threading.Lock()

    def _roll_window_if_elapsed(self) -> None:
        now = time.monotonic()
        if now - self._window_start >= self._WINDOW_SECONDS:
            self._window_start = now
            self._tokens_used = 0

    def adjust(self, delta_tokens: int) -> None:
        """Correct the current window by (actual - reserved). Negative
        refunds an over-reservation or a failed call."""
        with self._lock:
            self._roll_window_if_elapsed()
            self._tokens_used = max(0, self._tokens_used + delta_tokens)

    def acquire(self, estimated_tokens: int) -> None:
        while True:
            raise_if_cancelled()
            with self._lock:
                self._roll_window_if_elapsed()
                fits = self._tokens_used + estimated_tokens <= self.tokens_per_minute
                window_otherwise_empty = self._tokens_used == 0
                if fits or window_otherwise_empty:
                    self._tokens_used += estimated_tokens
                    return
                remaining = self._WINDOW_SECONDS - (time.monotonic() - self._window_start)
            # Sleep OUTSIDE the lock, in short slices so a cancel during
            # a long pacing wait is noticed within ~1 s, not a minute.
            time.sleep(min(max(remaining, 0.1), 1.0))


class _RequestPacer:
    """Spaces calls at least 60/rpm seconds apart, process-wide for one
    provider account and model. Token pacing cannot protect a
    requests-per-minute cap: a handful of tiny prompts is few tokens but
    many requests. Slots are reserved under the lock and slept for
    outside it, in short slices so a cancel is noticed within ~1 s."""

    def __init__(self, requests_per_minute: int):
        self.min_interval = 60.0 / requests_per_minute
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        while True:
            raise_if_cancelled()
            remaining = slot - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))


# --- Process-wide, per-(provider, API key) shared state -----------------
# _configure_llms builds a fresh client per request, so a bucket owned by
# the client would give every concurrent job its OWN full TPM budget and
# the account would receive N x the limit. The budget belongs to the
# provider account, so it lives here, keyed by provider + a key
# fingerprint (never the key itself).
_SHARED_LOCK = threading.Lock()
_SHARED_BUCKETS: dict[tuple[str, str, int], _TokenBucket] = {}
_SHARED_PACERS: dict[tuple[str, str, str, int], _RequestPacer] = {}
_QUOTA_BLOCKED_UNTIL: dict[tuple[str, str], float] = {}
_DEFAULT_DAILY_BLOCK_SECONDS = 30 * 60.0
# Providers count the reply (for a reasoning model, its thinking too)
# against TPM, but the bucket only sees the prompt up front. Observed
# live: ~7k tokens per call in total. Reserve headroom for the reply.
_OUTPUT_TOKEN_ALLOWANCE = 1500  # initial guess only; replaced by an EMA of real usage


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _shared_bucket(provider: str, api_key: str, tokens_per_minute: int) -> _TokenBucket:
    k = (provider, _key_fingerprint(api_key), tokens_per_minute)
    with _SHARED_LOCK:
        bucket = _SHARED_BUCKETS.get(k)
        if bucket is None:
            bucket = _SHARED_BUCKETS[k] = _TokenBucket(tokens_per_minute)
        return bucket


def _shared_pacer(provider: str, api_key: str, model: str, rpm: int) -> _RequestPacer:
    k = (provider, _key_fingerprint(api_key), model, rpm)
    with _SHARED_LOCK:
        pacer = _SHARED_PACERS.get(k)
        if pacer is None:
            pacer = _SHARED_PACERS[k] = _RequestPacer(rpm)
        return pacer


_RETRY_IN_RE = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def _parse_retry_seconds(message: str) -> float:
    """Reads Groq's 'Please try again in 33m2.448s' hint; falls back to
    a conservative default when absent or unparseable."""
    m = _RETRY_IN_RE.search(message)
    if m and any(m.groups()):
        h, mi, sec = m.groups()
        total = int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
        if total > 0:
            return total
    return _DEFAULT_DAILY_BLOCK_SECONDS


_OUTPUT_EMA: dict[tuple[str, str], float] = {}
_USAGE_TOTALS: dict[tuple[str, str], dict[str, int]] = {}
_EMA_ALPHA = 0.3


def usage_snapshot() -> dict[str, dict[str, int]]:
    """Cumulative provider-reported token usage since process start,
    per provider (for reconciling against the provider's dashboard)."""
    with _SHARED_LOCK:
        return {f"{p}:{fp}": dict(v) for (p, fp), v in _USAGE_TOTALS.items()}


def _reset_shared_state_for_tests() -> None:
    with _SHARED_LOCK:
        _OUTPUT_EMA.clear()
        _USAGE_TOTALS.clear()
        _SHARED_BUCKETS.clear()
        _SHARED_PACERS.clear()
        _QUOTA_BLOCKED_UNTIL.clear()


_USAGE = threading.local()  # per-thread: the last call's provider-reported usage


def _record_usage(prompt: int | None, completion: int | None, total: int | None) -> None:
    if total is None and prompt is None and completion is None:
        return
    p, c = int(prompt or 0), int(completion or 0)
    _USAGE.usage = {"prompt": p, "completion": c, "total": int(total if total is not None else p + c)}


class LLMUnavailableError(Exception):
    """Raised by `complete_with_retry` when a provider call failed on
    every retry attempt. This is the signal callers (Verifier,
    SemanticReasoner) catch to fail safe — mark the affected section
    as genuinely unavailable rather than crash the whole scan or
    silently show unverified/incomplete results as if they were
    complete. Wraps the last underlying exception so the real cause
    isn't lost."""

    def __init__(self, message: str, last_error: Exception):
        super().__init__(message)
        self.last_error = last_error


class LLMRequestTooLargeError(Exception):
    """Raised by `complete_with_retry` immediately — on the FIRST
    attempt, no retries spent — when the provider rejects a call
    specifically because this one payload is too big for a single
    request (see `_is_request_too_large`). This is deliberately NOT
    folded into the normal retry/backoff loop: retrying an unchanged,
    oversized payload can never succeed no matter how long you wait or
    how many attempts you spend, since waiting doesn't shrink the
    prompt. Backing off here would just waste the whole retry budget
    reproducing the identical failure three times (this is exactly
    what happened before this was added — a batch-size-100 run burned
    3 retries on a 413 that was never going to change).

    Distinct from `LLMUnavailableError` on purpose: this is the
    caller's problem to fix (shrink the payload and try again with
    less), not something a client-level retry can paper over — only
    the caller (e.g. `SemanticReasoner`, which knows it can drop rows
    from its batch) has enough context to actually shrink anything."""

    def __init__(self, message: str, last_error: Exception):
        super().__init__(message)
        self.last_error = last_error


class LLMClient(ABC):
    """`tokens_per_minute`, when set by a subclass, gates every call
    (both `complete` and `complete_with_retry`) through a `_TokenBucket`
    — pacing calls BEFORE they fire, so a burst of batches can't
    self-inflict a provider's rate limit in the first place. `None`
    (the default) disables pacing entirely — unset unless a subclass
    opts in with a real budget, so nothing sleeps unexpectedly for a
    client whose provider limit is unknown or untested."""

    _token_bucket: _TokenBucket | None = None

    _quota_scope: tuple[str, str] | None = None

    # Per-instance call accounting for the egress ledger. A "live" call
    # reached the provider (one successful completion); a "replayed" call
    # was answered from the replay cache and sent nothing anywhere.
    live_calls: int = 0
    replayed_calls: int = 0

    _request_pacer: "_RequestPacer | None" = None

    def _init_rate_limit(
        self,
        tokens_per_minute: int | None,
        provider: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Subclasses call this from `__init__` with their provider's
        TPM budget (or None to leave pacing off). With `provider` and
        `api_key` the bucket and the daily-quota circuit breaker are
        PROCESS-WIDE for that account, shared by every client/job."""
        if provider and api_key:
            self._quota_scope = (provider, _key_fingerprint(api_key))
            self._token_bucket = (
                _shared_bucket(provider, api_key, tokens_per_minute) if tokens_per_minute else None
            )
        else:
            self._token_bucket = _TokenBucket(tokens_per_minute) if tokens_per_minute else None

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Send a system + user prompt, return the model's text reply.
        A single, unretried attempt — implement the raw provider call
        here. Callers that want retry/backoff use
        `complete_with_retry` instead, which wraps this."""
        raise NotImplementedError

    def _paced_complete(self, system_prompt: str, user_prompt: str) -> str:
        """`complete`, but blocking first if the token bucket says this
        call would exceed the per-minute budget. Both `complete_with_retry`
        and a direct `complete_with_retry`-less caller go through this,
        since pacing needs to happen before EVERY attempt, not just the
        first — a retried call still costs the same tokens again."""
        # Reserve prompt + the EXPECTED reply (an EMA of real completion
        # sizes, seeded by _OUTPUT_TOKEN_ALLOWANCE), then settle against
        # the provider's reported usage. A failed call is refunded.
        # Usage is recorded and logged even with NO bucket (Gemini's
        # default) - accounting does not depend on pacing being on.
        scope = self._quota_scope
        bucket = self._token_bucket
        reserved = 0
        if bucket is not None:
            with _SHARED_LOCK:
                expected_out = (
                    int(_OUTPUT_EMA.get(scope, _OUTPUT_TOKEN_ALLOWANCE)) if scope else _OUTPUT_TOKEN_ALLOWANCE
                )
            reserved = _estimate_tokens(system_prompt + user_prompt) + expected_out
            bucket.acquire(reserved)
        if self._request_pacer is not None:
            self._request_pacer.acquire()
        raise_if_cancelled()
        _USAGE.usage = None
        try:
            text = self.complete(system_prompt, user_prompt)
        except Exception:
            if bucket is not None:
                bucket.adjust(-reserved)
            raise
        usage = getattr(_USAGE, "usage", None)
        if usage:
            if bucket is not None:
                bucket.adjust(usage["total"] - reserved)
            if scope:
                with _SHARED_LOCK:
                    prev = _OUTPUT_EMA.get(scope, _OUTPUT_TOKEN_ALLOWANCE)
                    _OUTPUT_EMA[scope] = (1 - _EMA_ALPHA) * prev + _EMA_ALPHA * usage["completion"]
                    tot = _USAGE_TOTALS.setdefault(scope, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})
                    tot["prompt"] += usage["prompt"]
                    tot["completion"] += usage["completion"]
                    tot["total"] += usage["total"]
                    tot["calls"] += 1
            _log.info(
                "llm usage provider=%s prompt=%d completion=%d total=%d reserved=%d",
                scope[0] if scope else "?", usage["prompt"], usage["completion"], usage["total"], reserved,
            )
        return text

    def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
        """Retry/backoff wrapper around `complete`, shared by every
        provider so resilience isn't reimplemented per subclass. Paces
        every attempt through the token bucket first (see
        `_paced_complete`) — pacing is the primary defense against a
        self-inflicted rate limit, retry/backoff is the fallback for
        the rarer provider-side blip that pacing can't prevent. Never
        lets a raw provider exception escape — after every attempt
        fails, raises `LLMUnavailableError` instead, so callers have
        one exception type to catch and fail safe on, regardless of
        which provider or underlying error caused it."""
        last_error: Exception | None = None
        raise_if_cancelled()
        # Replay cache (off unless LLM_REPLAY_CACHE_DIR is set): an
        # identical prompt to the same model returns the stored reply
        # before any pacing, quota check or network call.
        provider_name = self._quota_scope[0] if self._quota_scope else type(self).__name__
        structured = output_schemas.schema_name_for(system_prompt)
        cache_key = replay_cache.make_key(
            provider_name,
            (getattr(self, "_model", "") or "") + (f"|json_schema:{structured}" if structured else ""),
            system_prompt,
            user_prompt,
        )
        cached = replay_cache.lookup(cache_key)
        if cached is not None:
            self.replayed_calls = self.replayed_calls + 1
            return cached
        if self._quota_scope is not None:
            with _SHARED_LOCK:
                until = _QUOTA_BLOCKED_UNTIL.get(self._quota_scope)
            if until is not None and time.monotonic() < until:
                mins = max(1, int((until - time.monotonic()) / 60))
                raise LLMUnavailableError(
                    f"{self._quota_scope[0]} daily token quota is exhausted for this API key "
                    f"(shared by all scans on this server); not calling the provider again "
                    f"for ~{mins} min.",
                    RuntimeError("daily quota breaker open"),
                )
        for attempt in range(_MAX_ATTEMPTS):
            raise_if_cancelled()
            try:
                text = self._paced_complete(system_prompt, user_prompt)
                self.live_calls = self.live_calls + 1
                replay_cache.store(cache_key, provider_name, getattr(self, "_model", "") or "", text)
                return text
            except Exception as exc:  # noqa: BLE001 - deliberately broad:
                # any provider failure (busy server, rate limit, network
                # blip) gets the same fail-safe treatment, not just a
                # hand-picked subset of exception types — EXCEPT the
                # "payload too large" class below, which is structurally
                # different (see LLMRequestTooLargeError's docstring).
                if isinstance(exc, LLMRequestTooLargeError):
                    raise  # already classified (e.g. output budget exhausted); never retried
                if _is_request_too_large(exc):
                    raise LLMRequestTooLargeError(
                        f"Request rejected as too large on first attempt "
                        f"(not retried — retrying an unchanged oversized "
                        f"payload can't succeed): {exc}",
                        exc,
                    ) from exc
                last_error = exc
                if _is_daily_quota_exhausted(exc):
                    if self._quota_scope is not None:
                        with _SHARED_LOCK:
                            _QUOTA_BLOCKED_UNTIL[self._quota_scope] = (
                                time.monotonic() + _parse_retry_seconds(str(exc))
                            )
                    # A per-DAY token cap resets in tens of minutes to
                    # hours; backing off seconds and retrying cannot
                    # succeed, it only burns time. Fail fast.
                    raise LLMUnavailableError(
                        f"Provider daily token quota exhausted (not retried): {exc}",
                        exc,
                    ) from exc
                if attempt < _MAX_ATTEMPTS - 1:
                    time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
        raise LLMUnavailableError(
            f"LLM call failed after {_MAX_ATTEMPTS} attempts: {last_error}",
            last_error,  # type: ignore[arg-type]
        )


def _is_daily_quota_exhausted(exc: Exception) -> bool:
    text = str(exc).lower()
    return "tokens per day" in text or "(tpd)" in text or "requests per day" in text


class GroqClient(LLMClient):
    """Generator's provider — fast, cheap, suited to high-volume
    reasoning over samples.

    Default model (`openai/gpt-oss-20b`) is picked from what's actually
    available on the account this was tested against — Groq's model
    access varies by account, so if this 404s for you, run
    `scripts/check_llm_connectivity.py` to see your own key's list and
    update this default. `openai/gpt-oss-120b` is the larger, more
    capable sibling if 20b's quality isn't enough once real usage
    starts."""

    def __init__(self, model: str = "openai/gpt-oss-20b", tokens_per_minute: int | None = None):
        from groq import Groq

        self._client = Groq(api_key=groq_api_key())
        self._model = model
        # Defaults to a conservative budget under the observed
        # free-tier TPM cap (see app.config.groq_tokens_per_minute) —
        # this is what prevents the semantic reasoning loop's batches
        # from firing faster than Groq's own rate limit allows, rather
        # than firing them all immediately and relying on retry to
        # clean up the 429s afterward.
        self._init_rate_limit(
            tokens_per_minute if tokens_per_minute is not None else groq_tokens_per_minute(),
            provider="groq",
            api_key=groq_api_key(),
        )

    _structured_rejected = False

    def _extra_request_args(self) -> dict:
        # reasoning_effort is only valid for the gpt-oss family; sending
        # it to another model id would be rejected, so gate on the name.
        effort = groq_reasoning_effort()
        if effort and self._model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": effort}
        return {}

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        # temperature=0 — a reproduced bug: two scans of the identical
        # file could return different flags (and therefore a different
        # score) because neither the sampling nor the model call itself
        # was deterministic. Row sampling is now seeded from the
        # table's own content (see semantic_reasoning.py's
        # _content_seed); this is the other half — pinning the model
        # to its most deterministic setting so the SAME sampled rows
        # also get the SAME judgment call.
        #
        # `seed`, added alongside temperature=0: a content-derived seed
        # (see _deterministic_seed above) so the SAME prompt text also
        # requests the SAME seed from Groq's backend every time. This
        # is a best-effort mitigation, not a guarantee — Groq's own API
        # reference is explicit that "Determinism is not guaranteed"
        # even with a fixed seed (see docs/SCORING.md's "Live
        # determinism" limitation for the sourced quote). It costs
        # nothing and can only help, so there's no reason to leave it
        # unset.
        request = dict(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            seed=_deterministic_seed(system_prompt, user_prompt),
            **self._extra_request_args(),
        )
        schema_name = output_schemas.schema_name_for(system_prompt)
        if schema_name and self._model.startswith("openai/gpt-oss") and not self._structured_rejected:
            try:
                response = self._client.chat.completions.create(
                    **request,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": output_schemas.SCHEMAS[schema_name],
                        },
                    },
                )
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) != 400:
                    raise
                # The provider refused the schema (or its own validation
                # failed). Fall back to the plain request, and stop
                # trying structured output on this client.
                _log.warning("groq rejected structured output (%s); falling back to plain JSON", schema_name)
                self._structured_rejected = True
                response = self._client.chat.completions.create(**request)
        else:
            response = self._client.chat.completions.create(**request)
        u = getattr(response, "usage", None)
        if u is not None:
            _record_usage(
                getattr(u, "prompt_tokens", None),
                getattr(u, "completion_tokens", None),
                getattr(u, "total_tokens", None),
            )
        choice = response.choices[0]
        content = choice.message.content or ""
        if not content.strip():
            # An empty reply is otherwise indistinguishable from a bad
            # one downstream ("unparseable (len=0)"). Say WHY at WARNING
            # level (visible with default logging): a reasoning model
            # that spends its whole completion budget thinking returns
            # finish_reason=length with empty content. Counts only, no
            # row content.
            _log.warning(
                "groq empty content model=%s finish_reason=%s prompt_tokens=%s completion_tokens=%s "
                "reasoning_chars=%d",
                self._model,
                getattr(choice, "finish_reason", None),
                getattr(u, "prompt_tokens", None) if u is not None else None,
                getattr(u, "completion_tokens", None) if u is not None else None,
                len(getattr(choice.message, "reasoning", None) or ""),
            )
            if getattr(choice, "finish_reason", None) == "length":
                # The model spent its whole output budget reasoning and
                # wrote no answer. Retrying the same prompt would do the
                # same, so surface it as "this request is too big for the
                # output budget": callers already shrink the batch on
                # LLMRequestTooLargeError (SemanticReasoner halves it).
                raise LLMRequestTooLargeError(
                    "Model used its whole output budget reasoning and returned no answer "
                    "(finish_reason=length); batch is too large for it.",
                    RuntimeError("finish_reason=length"),
                )
        return content


class GeminiClient(LLMClient):
    """Verifier's provider — deliberately a different model family than
    the generator, so it isn't prone to the same blind spots when
    checking the generator's evidence."""

    _structured_rejected = False

    def __init__(
        self,
        model: str | None = None,
        tokens_per_minute: int | None = None,
        requests_per_minute: int | None = -1,
    ):
        from google import genai

        self._client = genai.Client(api_key=gemini_api_key())
        self._model = model or gemini_model()
        rpm = gemini_requests_per_minute() if requests_per_minute == -1 else requests_per_minute
        self._request_pacer = _shared_pacer("gemini", gemini_api_key(), self._model, rpm) if rpm else None
        # Defaults to None (pacing disabled) — see
        # app.config.gemini_tokens_per_minute for why: no TPM limit
        # has actually been observed against Gemini in this project
        # yet, only transient 503s, so there's no evidence-based number
        # to pace against. Set GEMINI_TOKENS_PER_MINUTE if that changes.
        self._init_rate_limit(
            tokens_per_minute if tokens_per_minute is not None else gemini_tokens_per_minute(),
            provider="gemini",
            api_key=gemini_api_key(),
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        from google.genai import types

        # temperature=0 + a content-derived seed — same determinism
        # mitigation as GroqClient.complete; see that method's comment
        # for the full reasoning. Gemini's own `seed` field is
        # documented even more thinly than Groq's ("set deterministic
        # generation seed", no explicit guarantee either way), and
        # Google's own community forum has reports of non-identical
        # output for an identical prompt + fixed seed + temperature=0
        # on at least one model — see docs/SCORING.md's "Live
        # determinism" limitation for the sourced account. Setting it
        # anyway costs nothing and gives the provider's own
        # infrastructure its best chance at reproducing a result.
        cfg = dict(
            system_instruction=system_prompt,
            temperature=0,
            seed=_deterministic_seed(system_prompt, user_prompt),
        )
        schema_name = output_schemas.schema_name_for(system_prompt)
        if schema_name and not self._structured_rejected:
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        **cfg,
                        response_mime_type="application/json",
                        response_json_schema=output_schemas.SCHEMAS[schema_name],
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if code != 400:
                    raise
                _log.warning("gemini rejected structured output (%s); falling back to plain JSON", schema_name)
                self._structured_rejected = True
                response = self._client.models.generate_content(
                    model=self._model, contents=user_prompt, config=types.GenerateContentConfig(**cfg)
                )
        else:
            response = self._client.models.generate_content(
                model=self._model, contents=user_prompt, config=types.GenerateContentConfig(**cfg)
            )
        um = getattr(response, "usage_metadata", None)
        if um is not None:
            _record_usage(
                getattr(um, "prompt_token_count", None),
                getattr(um, "candidates_token_count", None),
                getattr(um, "total_token_count", None),
            )
        return response.text or ""

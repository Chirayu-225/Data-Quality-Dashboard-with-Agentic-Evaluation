"""
Configuration — API keys and other secrets come from environment
variables only, never hardcoded and never committed. See .env.example
for what needs to be set locally.
"""

from __future__ import annotations

import os


class ConfigError(RuntimeError):
    """Raised when required configuration (like an API key) is missing."""


def _get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Set it as an environment variable "
            f"(see .env.example) before using anything that needs it."
        )
    return value


def groq_api_key() -> str:
    return _get_env("GROQ_API_KEY")


def gemini_api_key() -> str:
    return _get_env("GEMINI_API_KEY")


def _get_optional_int_env(name: str, default: int | None) -> int | None:
    value = os.environ.get(name)
    if not value:
        return default
    return int(value)


def groq_tokens_per_minute() -> int | None:
    """Groq's own free-tier default is conservative-below-observed,
    not the account's real cap — the real cap depends on account/tier
    and Groq's error messages report the actual live number when hit
    (e.g. "Limit 8000... TPM"), which is the source of truth. Override
    with GROQ_TOKENS_PER_MINUTE if your account's real limit differs
    (check https://console.groq.com/settings/billing). 7000 is a
    deliberate safety margin under an observed 8000 TPM free-tier cap,
    not itself Groq's published number."""
    return _get_optional_int_env("GROQ_TOKENS_PER_MINUTE", 7000)


def groq_reasoning_effort() -> str | None:
    """Reasoning effort requested from Groq's gpt-oss models. With the
    provider default the model can spend its whole output budget
    thinking and return an empty answer (finish_reason=length), seen on
    a live 45-row batch. "low" fixed it. Set GROQ_REASONING_EFFORT to
    low/medium/high, or "default" to send no setting at all."""
    value = os.environ.get("GROQ_REASONING_EFFORT", "low").strip().lower()
    if value in ("", "default", "none"):
        return None
    if value not in ("low", "medium", "high"):
        raise ConfigError("GROQ_REASONING_EFFORT must be low, medium, high or default.")
    return value


def gemini_tokens_per_minute() -> int | None:
    """Unlike Groq, no TPM limit has actually been observed against
    Gemini yet in this project (failures seen so far were transient
    503s, not 429 rate limits) — so this defaults to None (pacing
    disabled) rather than guessing a number that might be wrong in
    either direction. Google doesn't publish free-tier numbers in
    their docs; check https://aistudio.google.com/rate-limit for your
    account's actual limit and set GEMINI_TOKENS_PER_MINUTE if you
    start seeing 429s from Gemini too."""
    return _get_optional_int_env("GEMINI_TOKENS_PER_MINUTE", None)


def gemini_model() -> str:
    """Model the verifier uses. Default is gemini-3.5-flash-lite: on the
    free tier tested here gemini-3.6-flash returned sustained 503s and a
    20-requests/day cap, while 3.5-flash-lite answered 8/8 calls (plain
    and strict-schema) with identical replies. One prompt, 8 runs each:
    a spot check, not a benchmark. Override with GEMINI_MODEL."""
    return os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"


def gemini_requests_per_minute() -> int | None:
    """Requests-per-minute cap the Gemini client paces itself to. The
    free tier was observed at 5 requests/minute per model (a 429 naming
    GenerateRequestsPerMinutePerProjectPerModel-FreeTier, limit 5), so
    that is the default; a paid key can raise it. 0 or "off" disables
    request pacing. Override with GEMINI_REQUESTS_PER_MINUTE."""
    raw = os.environ.get("GEMINI_REQUESTS_PER_MINUTE", "").strip().lower()
    if raw in ("0", "off", "none"):
        return None
    return int(raw) if raw else 5


def max_upload_bytes() -> int:
    """Hard per-file size cap, enforced while STREAMING the upload (see
    main.py's `_read_upload_with_size_cap`) rather than after reading
    the whole thing into memory — a file over the cap never gets fully
    buffered in the first place. 20 MB is a reasonable default for a
    prototype demo; override with MAX_UPLOAD_BYTES if a real dataset
    legitimately needs more. Not tuned against any real deployment
    traffic — a judgment call, same honesty the scoring spec gives the
    severity-cap constant."""
    return _get_optional_int_env("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)


def max_semantic_reasoning_rows() -> int:
    """Above this many rows, semantic reasoning requires an explicit
    opt-in (`allow_large_semantic_scan=true` on the request) rather than
    running by default — see main.py's enforcement. Full-table LLM
    coverage makes cost scale with file size; this keeps that scaling
    bounded by default instead of unbounded, without blocking large
    files from deterministic-only scanning. 5000 rows is a judgment
    call sized for a free-tier demo budget, not derived from any
    specific cost target — override with MAX_SEMANTIC_REASONING_ROWS."""
    return _get_optional_int_env("MAX_SEMANTIC_REASONING_ROWS", 5000)


def scan_job_workers() -> int:
    """Max scan jobs executing at once (a bounded worker pool, not a
    thread per submission — see app/jobs.py). Each running job holds a
    paced stream of LLM calls, so this mostly bounds provider-rate and
    memory pressure. Override with SCAN_JOB_WORKERS."""
    return max(1, _get_optional_int_env("SCAN_JOB_WORKERS", 2) or 2)


def scan_job_max_pending() -> int:
    """Cap on jobs that are queued OR running at once. Submissions past
    this get a 429 instead of piling up unboundedly — the server has no
    auth yet, so anyone who can reach it could otherwise submit jobs
    until it fell over. Override with SCAN_JOB_MAX_PENDING."""
    return max(1, _get_optional_int_env("SCAN_JOB_MAX_PENDING", 8) or 8)


def scan_job_ttl_seconds() -> int:
    """How long a FINISHED (done/error) job's result stays retrievable
    before it's evicted. Results hold unmasked flagged rows, so they
    shouldn't linger in memory indefinitely. Override with
    SCAN_JOB_TTL_SECONDS (default one hour)."""
    return max(1, _get_optional_int_env("SCAN_JOB_TTL_SECONDS", 3600) or 3600)


def groq_tokens_per_day() -> int:
    """Groq free-tier daily token cap (observed 200,000 for
    openai/gpt-oss-20b on_demand). Used only for the pre-scan warning;
    override with GROQ_TOKENS_PER_DAY if your tier differs."""
    return _get_optional_int_env("GROQ_TOKENS_PER_DAY", 200000) or 200000


# Observed live: ~7,000 tokens per 45-row call (prompt + the model's
# reasoning/output), far above the input-only arithmetic estimate.
OBSERVED_TOKENS_PER_SEMANTIC_CALL = 7000


def scan_job_abandon_seconds() -> int:
    """A queued/running job nobody has POLLED for this long is
    auto-cancelled (0 disables). A refreshed tab resumes polling within
    seconds; a closed tab never does, so this stops abandoned scans from
    spending provider quota without affecting refreshes. Must exceed the
    browser's worst-case background-tab timer throttling (~1/min)."""
    return _get_optional_int_env("SCAN_JOB_ABANDON_SECONDS", 180) or 0


def _get_optional_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def psi_warn_threshold() -> float:
    """PSI at/above this is a 'warning' drift band. 0.10 is the commonly
    cited heuristic; sources disagree on the upper edge, so both edges are
    configurable (PSI_WARN_THRESHOLD / PSI_CRITICAL_THRESHOLD) and are
    heuristics, not universal truth."""
    return _get_optional_float_env("PSI_WARN_THRESHOLD", 0.10)


def psi_critical_threshold() -> float:
    """PSI at/above this is a 'critical' drift band (default 0.20)."""
    return _get_optional_float_env("PSI_CRITICAL_THRESHOLD", 0.20)


def local_review_enabled() -> bool:
    """Local value review (no LLM, no network) is the default semantic
    tier. Set LOCAL_REVIEW=0 to turn it off (e.g. to reproduce the
    pre-local-review scores)."""
    return os.environ.get("LOCAL_REVIEW", "1").strip().lower() not in ("0", "false", "off", "no")

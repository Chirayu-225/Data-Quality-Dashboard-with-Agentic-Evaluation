# Provider limits and model choice (observed, not promised)

Everything here was observed on one developer's free-tier keys on
2026-10-13. Providers change limits without notice; re-check before relying
on any number, and treat each line as "what happened", not "what is
guaranteed".

## Gemini (verifier)

| Observation | Evidence |
|---|---|
| `gemini-3.6-flash`: 20 requests/day on the free tier | 429 `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit 20 |
| `gemini-3.6-flash`: 5 requests/minute | 429 `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, limit 5 |
| `gemini-3.6-flash`: sustained 503 "high demand" over several hours | 0 of 18 later calls succeeded |
| `gemini-3.5-flash-lite`: 8/8 calls succeeded, plain and strict-schema, identical replies | `scripts/check_live_determinism.py`, one prompt, 8 runs each |
| Quotas are counted per model | the 429 names the model |
| `gemini-3.5-flash-lite`: 24 calls in one day without a quota error | so its free-tier daily cap is above 20 on this key |

Consequences in the code:
- The verifier default is `gemini-3.5-flash-lite` (`GEMINI_MODEL` overrides it).
- The Gemini client spaces requests to 5 per minute (`GEMINI_REQUESTS_PER_MINUTE`;
  `0` or `off` disables it, for example on a paid key).
- When the verifier is unavailable, flags stay unverified and score at weight 0.5.

## Groq (generator)

`openai/gpt-oss-20b`, 10 runs per prompt, temperature 0, seed pinned:
- rows with problems: identical text and decisions in 10/10
- all-clean table: identical in 10/10
- distinct values (company column): decisions identical in 8/10, confidence spread up to 0.05

This is why scans that must be reproducible use the replay cache
(`LLM_REPLAY_CACHE_DIR`): a live rescan of the same column can disagree
with itself.

## Strict JSON-schema output (`LLM_STRUCTURED_OUTPUT=1`)

- Groq documents strict mode for `openai/gpt-oss-20b`; it accepted the schema on all 30 calls (3 prompts x 10 runs), no fallback.
  Decisions were identical in 10/10 runs on every prompt, including distinct values (80% without the schema). Text
  identity went the other way on the all-clean prompt (60%, confidence spread 0.05). One trial each, so this is
  within what run-to-run noise could produce; it is not evidence that the schema improves stability.
- Gemini `gemini-3.5-flash-lite` accepted the schema on 8/8 calls.
- If a provider rejects a schema with HTTP 400, the client falls back to a
  plain request and stops trying structured output on that client.
- The schema guarantees shape, not correctness or identical text.

## Replay cache directory

`LLM_REPLAY_CACHE_DIR` holds SHA-256 hashes of prompts (never the prompts)
and the model's reply text. A reply can quote masked row values in its
reasons, so protect the directory like scan output. Off unless set.

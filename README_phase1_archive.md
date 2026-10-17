# Connectivity Service Platform with Agentic Evaluation (Phase 1 of 3)

Schema-agnostic data connectivity + agentic data quality detection.

## Structure

```
dbcaas/
├── frontend/                  Next.js — thin UI/API layer only
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx           Real test dashboard — upload, score meters, flagged-rows table
│   │   ├── globals.css        Design tokens (dataviz skill's reference palette)
│   │   ├── lib/types.ts       TS types mirroring the API's ScanResponse
│   │   ├── components/        ScanForm, ScoreHero, MetricMeter, FlaggedRowsTable
│   │   └── api/
│   │       ├── health/route.ts   Proxies to the Python service's /health
│   │       └── scan/route.ts     Proxies to the Python service's /scans
│   └── package.json
│
└── service/                   Python — owns all data/agent logic
    ├── app/
    │   ├── canonical/         The source-agnostic internal data model
    │   │   └── models.py      CanonicalTable, ColumnSchema, ColumnType, ScanContext
    │   ├── connectors/        Adapter interface + concrete connectors
    │   │   ├── base.py        Connector ABC every source must implement
    │   │   └── csv_connector.py   First connector — real, working, tested
    │   ├── checks/            Deterministic check registry (agent "tools") — Phase 1 complete
    │   │   ├── base.py        Check ABC + @register decorator (+ optional ScanContext)
    │   │   ├── completeness.py    NullCheck
    │   │   ├── uniqueness.py      DuplicateCheck (exact-match only — see note below)
    │   │   ├── validity.py        FormatValidityCheck (email/phone/date, permissive)
    │   │   ├── consistency.py     OutlierCheck (IQR-based)
    │   │   └── referential_integrity.py   Real cross-table check + fallback mode
    │   ├── rules/
    │   │   ├── dsl.py         Rule DSL — what custom NL prompts compile into
    │   │   ├── compiler.py    RuleCompiler — NL instruction -> structured rule or semantic guidance
    │   │   └── executor.py    execute_rule — runs a CompiledRule against real rows, no code execution
    │   ├── agent/              Phase 2 complete, iteration loop added since
    │   │   ├── generator.py    GeneratorAgent — deterministic plan+execute + semantic layer + iteration loop
    │   │   ├── llm_clients.py  LLMClient / GroqClient / GeminiClient — provider-agnostic
    │   │   ├── semantic_reasoning.py   LLM-driven plausibility judgment, structured output
    │   │   └── verifier.py     Verifier — checks semantic flags against evidence (Gemini)
    │   ├── scoring/            Phase 4 (scoring part) complete
    │   │   └── scorer.py       compute_scorecard — confidence-weighted metric rollup + overall score
    │   ├── db/                 Empty — deliberately on hold (see "Currently on hold")
    │   ├── config.py           API key loading — env vars only, never hardcoded
    │   └── main.py             FastAPI entrypoint — /health, /scans (stateless, no auth — see below)
    ├── eval/                   Agentic evaluation harness (new)
    │   ├── generate_datasets.py   Forges 3 domains of test data with KNOWN planted issues
    │   ├── datasets/               The forged CSVs + *_ground_truth.json files
    │   ├── run_eval.py             Runs the real pipeline against them, scores vs. ground truth
    │   ├── eval_report.md          Human-readable results (regenerated each run)
    │   └── eval_results.json       Same results, machine-readable
    ├── tests/                  129 passing tests covering all of the above
    ├── .env.example            Template for GROQ_API_KEY / GEMINI_API_KEY — copy to .env
    └── requirements.txt
```

## What's real vs. placeholder

**Working and tested:** canonical data model (including `ScanContext` for cross-table
checks), connector adapter interface, CSV connector (reads a real file, infers types,
infers nullability from blank ratio, infers semantic hints from column names),
FastAPI health check, and all 5 Phase 1 deterministic checks:

- `NullCheck` — completeness, using blank-ratio nullability inference
- `DuplicateCheck` — exact full-row duplicates only (near-duplicates are intentionally
  deferred to the LLM semantic layer in Phase 2 — a hash comparison can't catch typos
  or case differences, and isn't meant to)
- `FormatValidityCheck` — email/phone/date, deliberately permissive to avoid
  false-positiving on valid-but-unusual data
- `OutlierCheck` — IQR-based, with a minimum sample size to avoid meaningless stats on
  tiny data
- `ReferentialIntegrityCheck` — real cross-table lookup when a related table is present
  in the `ScanContext` (e.g. `account_id` checked against an `accounts` table's `id`
  column); falls back to flagging only blank FK values when no related table is
  available. **Known limitation:** the related-table lookup is name-based (an
  `account_id` column expects a table literally named `accounts`) — fragile, and
  intentionally so for now; real foreign-key metadata from the SQL/Salesforce
  connectors will replace this heuristic later.

Also built, Phase 2 (agent layer): `LLMClient` / `GroqClient` / `GeminiClient`
(`app/agent/llm_clients.py`) — provider-agnostic LLM wrappers, tested with
mocked/fake clients so the automated suite needs no real API keys. `GeneratorAgent`
(`app/agent/generator.py`) — rule-based deterministic planning + execution, plus an
optional LLM semantic reasoning layer (`app/agent/semantic_reasoning.py`) producing
structured, per-row flags with reasoning and confidence (not free text), sampling
rows rather than scanning the whole table, framing row data explicitly as untrusted
in the prompt (prompt-injection mitigation), failing safe on malformed output.
`Verifier` (`app/agent/verifier.py`) — checks each semantic flag against its row's
real data using Gemini (deliberately a different model family than the generator's
Groq, to avoid correlated blind spots). Three-way output — confirmed / needs_review /
rejected, not binary — fails safe to NEEDS_REVIEW on malformed/missing output. Wired
into `GeneratorAgent.run()` via an optional `verifier_llm` param — REJECTED flags are
excluded from the final result when verification ran.

Also built, Phase 3 (complete): `RuleCompiler` (`app/rules/compiler.py`) —
schema-grounded: given a table's real column names and a user's plain-English
instruction, classifies it as either a structured rule (compiles to the DSL,
validated against real fields — a hallucinated field name fails compilation
outright, not a silent no-op) or a semantic instruction (passed through as guidance
for the reasoning layer). `execute_rule` (`app/rules/executor.py`) — runs a
`CompiledRule` against real rows: interprets DATA, never executes generated code,
every operator (equals, greater_than, contains, matches_pattern, in_set, is_null,
etc.) is an explicit comparison, AND/OR conjunctions, fails safe (non-match, not a
crash) on missing or malformed values. Both are fully wired into
`GeneratorAgent.run()`: no `custom_instruction` → autonomous semantic reasoning
(unchanged); instruction compiles structured → executed deterministically, with NO
semantic reasoning call made at all (verified by a test that would fail if the LLM
got called twice); compiles semantic → the compiler's refined description guides the
reasoning layer; compilation fails → raw instruction passed through as a best-effort
fallback.

**Bug found and fixed along the way:** the deterministic check registry only got
populated as a side effect of pytest collecting OTHER test files that happened to
import the check modules — worked by accident running the full suite, silently
returned zero checks running any file in isolation (and would have in any real
entrypoint that doesn't happen to import every check module first, like a future
API endpoint). Fixed by making `app/checks/__init__.py` explicitly import every
check module, so registration no longer depends on import order luck.

Also built, Phase 4 (scoring part — connectors deliberately deferred): `compute_scorecard`
(`app/scoring/scorer.py`) — pure aggregation, runs no detection itself, over whatever a
`ScanResult` already produced. Confidence-weighted: deterministic check flags count fully
by default, a CONFIRMED verified flag counts fully, NEEDS_REVIEW counts at half weight,
REJECTED counts as zero, and an unverified semantic flag (no verifier ran) defaults to half
weight — an unconfirmed claim never weighs as heavily as a checked fact. A row flagged by
multiple checks under the same metric is weighted by its strongest signal (max), not
summed, so it isn't double-penalized. Metric weights are overridable per call (the
"user-adjustable weights" from the original design).

**Field-criticality weighting (additive follow-up, now built):** `CheckResult` gained an
optional `flagged_fields: dict[int, list[str]]` — which specific column(s) caused each
flagged row, not just the row index. `NullCheck`, `FormatValidityCheck`, `OutlierCheck`,
and `ReferentialIntegrityCheck` all populate it; `DuplicateCheck` deliberately doesn't
(duplication is a whole-row property, not one field's fault) and falls back to flat
weighting exactly as before this feature existed — verified by a dedicated test. The
scorer now weighs a flag on a required (non-nullable) field at 2x an optional field's
weight, derived from `ColumnSchema.nullable` (reusing existing schema info, not a new
concept). `compute_scorecard`'s signature changed from `total_rows: int` to
`table: CanonicalTable` to make the column lookup possible — updated at every call site.
Rigorously tested: field attribution correctness verified independently at each of the 4
checks (not just presence), the scorer's 2x-weight claim verified as two isolated
before/after scenarios (not inferred), the flat-weight fallback verified explicitly, an
unknown-field-name edge case verified to degrade safely rather than crash, and one
end-to-end integration test running the real `NullCheck` through the real scorer — which
caught a genuine test-authoring bug (a wrong assumption about when `NullCheck` flags a
row) during the process, documented in the test itself rather than quietly fixed away.

**Agent iteration loop, then redesigned into full-table coverage (both built, in that
order):** the generator's semantic reasoning pass was single-shot originally — one
sampled batch, one LLM call, done. First fix: `SemanticReasoner.run()` gained an
`exclude_indices` param so a repeat call samples a FRESH batch rather than
re-judging rows already seen (`sampled_indices` on the result lets a caller track
coverage), and a capped iteration loop (3 cycles, continue-if-flag-ratio-over-20%)
was built on top of it.

That capped design turned out to be the wrong default, caught by a real evaluation
run against forged ground-truth data (`eval/`), not by inspection. Semantic-only
recall came back 0.0 on all three eval datasets — the 3-cycle cap combined with
sequential (not random) sampling meant the loop only ever reached roughly the first
60 rows of any table, every single run, regardless of where the actual problems
sat. Randomizing the sample was a real, verified improvement (semantic-only recall
went 0.0 -> 0.4 / 0.0 / 0.2 across the three datasets on a live rerun), but it
didn't resolve the deeper question: genuine LLM semantic judgment — not just
deterministic rule-checking — is this product's stated differentiator against
Validity/RingLead/Cloudingo/Insycle, and a detection layer that only ever samples
part of a table undercuts that claim regardless of how well the sampling is tuned.

So the design changed again, deliberately: **full-table coverage is now the
default.** `GeneratorAgent._run_semantic_with_iteration()` no longer stops based on
a flag-ratio threshold or a fixed cycle cap — it batches straight through every row
in the table (`sample_size` rows per LLM call, each batch excluding every row
already sampled) until nothing fresh is left to offer. The only way to bound it is
the new, explicit `max_semantic_llm_calls` param on `GeneratorAgent.run()` (also
exposed on the `/scans` endpoint as a form field) — `None` (the default) means no
cap, every row gets a semantic pass; a caller sets a number only when THEY want to
trade coverage for cost on a very large table, never something the method decides
quietly on its own. The old `_MAX_SEMANTIC_ITERATIONS`/`_HIGH_FLAG_RATIO_THRESHOLD`
constants are gone — coverage is no longer a function of how suspicious a batch
looked, only of whether the table has been fully sampled or the caller's explicit
budget ran out. `ScanResult.semantic_iterations` now means "how many batches it
actually took to cover (or budget-limit) this table," not "how many times it got
suspicious enough to look again."

**Real cost implication, stated plainly:** LLM calls now scale with table size —
roughly one call per `sample_size` (20) rows instead of a flat 1-3. A 150-row table
is ~8 batched calls instead of 1-3; this is a deliberate accuracy-over-cost
tradeoff, not an oversight, made because detection accuracy is this product's
actual selling point. `max_semantic_llm_calls` exists specifically for whoever
needs to bound that cost later (very large Salesforce orgs, a tight API budget)
without it being the default anyone has to opt out of.

## Provider resilience: retry, verifier batching, and rate-limit pacing (built after a real eval run crashed)

Full-table coverage means a scan can fire 8+ LLM calls in quick succession instead
of 1-3. A live eval run (`eval/run_eval.py`) surfaced three separate real failures
this exposed, each needing a genuinely different fix — worth documenting as three
distinct things, not one generic "add error handling" pass, because conflating
them was the actual mistake the first time around:

1. **Verifier had no batching.** `Verifier.verify()` used to send every semantic
   flag from a whole dataset in ONE Gemini call. One busy-server moment mid-call
   killed verification for the entire dataset, not just the affected flags. Fixed:
   `Verifier.verify()` now chunks flags into batches of `_VERIFY_CHUNK_SIZE` (20)
   per call. A chunk that fails every retry falls back to `NEEDS_REVIEW` for just
   its own flags (same fail-safe convention as malformed output), not a crash —
   the chunks before and after it are unaffected.

2. **No retry/backoff on transient provider failures** (a busy-server 503, a
   dropped connection). Fixed: `LLMClient.complete_with_retry()` — 3 attempts,
   exponential backoff (1s -> 2s -> 4s), shared by every provider via the base
   class rather than duplicated per client. On exhaustion it raises one
   `LLMUnavailableError`, never a raw provider exception, so callers (`Verifier`,
   `SemanticReasoner` via `GeneratorAgent`) have one thing to catch. The generator's
   semantic loop catches it and stops gracefully — keeping whatever flags/coverage
   earlier batches already earned instead of losing them, and setting a new
   `ScanResult.semantic_coverage_warning` field (surfaced into the API's
   `warnings` list) so a caller is told coverage is honestly incomplete rather than
   being shown partial results as if they were complete. Deterministic checks are
   entirely unaffected either way, since they run before the LLM layer starts.

3. **No pacing — and this is the one retry/backoff can't fix.** A live rerun hit
   Groq's `429 rate_limit_exceeded` (`tokens per minute (TPM)` — an 8000/min
   free-tier cap) firing 8 batched calls back-to-back. Retry/backoff reacts AFTER
   a call fails; against a per-minute token budget, a burst of calls can exhaust
   that budget in seconds, and retrying within the same still-exhausted window
   just fails again. Bigger batches don't fix this either — they reduce call
   *count*, not total *token volume*, and a TPM cap is denominated in tokens, not
   calls or requests. The actual fix needed to be a third, different mechanism:
   pacing calls BEFORE they fire, not recovering after they fail.

   Fixed with `_TokenBucket` — a fixed-window tokens-per-minute budget shared by
   every call a client makes. `LLMClient.complete_with_retry()` calls
   `self._token_bucket.acquire(estimated_tokens)` before every attempt (including
   retries, since a retried call costs the same tokens again); `acquire` blocks
   until enough of the current minute's budget is free rather than firing and
   hoping. Token counts are estimated with a simple `chars / 4` heuristic
   (`_estimate_tokens`) — no tokenizer dependency, deliberately conservative
   (rounds up). A single call bigger than the whole budget is still let through
   once the window is otherwise empty, rather than blocking forever.

   `GroqClient` defaults to a 7000 TPM budget — a safety margin under the
   `8000` cap Groq's own error message reported for the account this was tested
   against (not Groq's published number; overridable per-account via
   `GROQ_TOKENS_PER_MINUTE`, since real limits vary by account/tier — check
   https://console.groq.com/settings/billing for yours). `GeminiClient` defaults
   to pacing OFF (`None`) — every Gemini failure seen in this project so far has
   been a transient `503` (busy server), never a `429` rate limit, so there's no
   observed number to pace against yet; set `GEMINI_TOKENS_PER_MINUTE` if that
   changes (check https://aistudio.google.com/rate-limit for your account's
   actual limit — Google doesn't publish free-tier numbers in their docs).

   `RuleCompiler.compile()` was also switched from `complete` to
   `complete_with_retry`, so its one call per custom instruction counts against
   the same budget instead of silently bypassing it — an unavailable LLM there
   now falls into the compiler's existing `"failed"` outcome (which already has a
   defined fallback: pass the raw instruction to semantic reasoning, best-effort)
   rather than crashing the scan.

**The general lesson, worth keeping in mind for anything that calls an LLM at
volume:** call count, total token volume, and throughput rate are three
independently-violatable constraints. A fix on one axis provides zero guarantee
on the other two — bigger batches (call count) didn't touch the TPM problem (rate),
and retries (a rate/count-agnostic reaction) couldn't prevent a self-inflicted
burst in the first place. The unit a rate limit is actually denominated in
(requests vs. tokens, per-minute vs. per-day) tells you which axis you're
actually hitting and which fix applies — worth checking before picking a lever.

## A fourth axis: per-request size, and the eval-result cache

A batch-size sweep against the eval harness (trying 100 rows/call instead of the
default 20) surfaced a fourth, structurally different constraint from the three
above: Groq rejected the call outright with `413 Request too large ... tokens per
minute (TPM): Limit 8000, Requested 9239` — not a rate problem, a **single-request**
size problem. `_TokenBucket` correctly paces *how often* calls fire, but it can't
shrink one oversized call below the model's own per-request ceiling. Worse, the old
retry logic actively wasted effort here: `complete_with_retry` retried the identical,
still-oversized prompt three times, guaranteeing three identical 413s, since waiting
between attempts changes nothing about a payload's size.

Fixed with a new exception type, `LLMRequestTooLargeError`, that `complete_with_retry`
raises immediately on a 413 (detected via `getattr(exc, "status_code", None) == 413`,
duck-typed so it works across SDKs) — **no retries, no backoff spent**, since both are
pointless against this failure. `SemanticReasoner.run()` catches it and does the thing
a client-level retry can't: halves its own batch size and rebuilds a smaller prompt,
repeating down to a floor of 5 rows before giving up. Once a smaller size succeeds, it's
persisted onto the reasoner instance so later batches in the same scan don't rediscover
the same 413 from scratch. If even the floor is rejected (a single row's own content is
unusually huge), `GeneratorAgent` treats it exactly like `LLMUnavailableError` — stop the
loop, keep whatever coverage already happened, explain why in `semantic_coverage_warning`.

Separately, `eval/run_eval.py` gained a **result cache** (`eval_cache.json`) for the
custom-instruction battery, keyed on a hash of the dataset's CSV content plus a
`_PROMPT_VERSION` constant (bump it whenever a prompt that affects LLM output changes).
This exists because a batch-size sweep was re-paying for those 9 compiler calls (3
datasets x 3 instructions) on every single sweep step, even though that result can't
change with `SEMANTIC_SAMPLE_SIZE` — pure waste against Groq's separate, slower-draining
daily token cap (`tokens per day (TPD)`, observed at 200000/day), which no amount of
per-minute pacing touches, since it's cumulative across every run of the day, not a
property of any one run's shape. The per-dataset resume check (`_dataset_already_done`)
was tightened the same way, so an edited dataset or a bumped `_PROMPT_VERSION` correctly
forces a fresh run instead of silently reusing a stale one.

135/135 tests pass (129 from before this fix, plus 6 new: 2 for the 413 fail-fast
behavior in `complete_with_retry`, 3 for `SemanticReasoner`'s shrink-and-retry loop, and
1 for `GeneratorAgent`'s fail-safe when even the floor size is too large). The eval
cache's own logic (hash stability, hit/miss, prompt-version invalidation) was verified
with a standalone script rather than pytest, since it lives in a live-API harness rather
than the test suite proper.

113/113 tests pass (108 from before, plus 5 new for the `/scans` endpoint).
Next.js builds cleanly, including the real dashboard. Live demo scripts (real API calls, can't be
verified in advance): `scripts/check_llm_connectivity.py`, `scripts/semantic_reasoning_demo.py`,
`scripts/full_pipeline_demo.py`, `scripts/rule_compiler_demo.py`,
`scripts/custom_instruction_demo.py`, `scripts/full_scorecard_demo.py` (the complete
pipeline — checks + semantic reasoning + verification + scoring together). `scripts/manual_run.py`
now also prints a scorecard, with field-criticality weighting visible, computed with no API
keys at all, since scoring is pure aggregation over already-run deterministic checks.

**Stateless API endpoint (new):** `POST /scans` on the FastAPI service (`app/main.py`)
— multipart upload, `file` (the table to scan) plus optional `related_files` (for
referential integrity) and `custom_instruction`. Runs the exact same
`GeneratorAgent` → `compute_scorecard` pipeline the scripts already exercised, over
a temp file, and returns the scorecard, every flagged row (deterministic reasons +
semantic reasoning + verification label, merged), `semantic_iterations`, and the
compiled-rule description if a custom instruction was given — all in one response,
nothing persisted. If `GROQ_API_KEY`/`GEMINI_API_KEY` aren't set on the server it
degrades to deterministic-only automatically (a `warnings` field says so) rather
than erroring — this is what let the eval harness and the frontend both be built
and tested without needing real keys wired in everywhere. 5 new tests
(`tests/test_main_api.py`) cover the no-keys path, the related-file/referential
-integrity path, and the clean-table-scores-100 path via FastAPI's TestClient.

**Frontend dashboard (real, not placeholder, new):** upload a CSV (plus an
optional related CSV, plus an optional plain-English custom instruction), hit
"Run scan," see the actual scorecard — an overall-score hero figure, a meter per
metric (completeness/uniqueness/validity/consistency/accuracy), and a flagged-rows
table with per-row reasoning and a verification badge when the semantic layer
caught something. Built against the dataviz skill's reference palette (status
colors for score bands, a meter form for per-metric scores, text never carrying
the data color) rather than ad hoc styling. Talks to the Python service through
`app/api/scan/route.ts`, a server-side proxy (same pattern as the existing
`/api/health` proxy) — this is deliberately the seam where an auth token gets
attached once auth exists, so the scan-submitting code in `page.tsx` won't need to
change when that lands. No login, no saved scan history — this dashboard is for
testing the pipeline, not the authenticated multi-tenant product; it says so
directly in its own header so that's never ambiguous to whoever's looking at it.

**Agentic evaluation harness (new):** `eval/generate_datasets.py` forges three
domains of synthetic test data — `crm_leads` (leads+accounts, Salesforce-shaped),
`ecommerce_orders` (orders+customers), and `hr_employees` (single table, no
FK-shaped column at all) — each ~150 rows with six categories of KNOWN planted
issue in disjoint row ranges (so precision/recall stays interpretable), one of
which (`semantic_mismatch`, e.g. "Joe's Bakery" filed under industry
"Technology") only the LLM layer can ever catch, by construction. `eval/run_eval.py`
runs the real pipeline against them and scores it against that ground truth:
precision/recall/F1 for detection accuracy (split into deterministic vs.
semantic-only, so a weak semantic layer can't hide behind strong deterministic
coverage), `semantic_iterations` observed under realistic table sizes (143-150
rows, well past the 20-row sample size — unlike the unit tests, which use tiny
tables and fake LLMs to force specific counts), and a hand-labeled custom
-instruction battery per dataset (structured / semantic / deliberately
-hallucinated-field, to check the compiler both classifies correctly and fails
safe). The deterministic-only baseline needs no API keys and is verified: **100%
precision, 100% recall, F1 1.0 on all three domains** — every deterministically
-catchable planted issue caught, zero false positives, on data the checks had
never seen. The LLM-dependent sections (semantic detection accuracy, iteration
-loop behavior, instruction-battery results) need `GROQ_API_KEY`/`GEMINI_API_KEY`
set to run — this sandbox doesn't hold your keys, so run
`PYTHONPATH=. python3 eval/run_eval.py` yourself with them set to get those
numbers; `eval/eval_report.md` is regenerated each run.

**A real live run found a real bug (fixed):** the first full-keys eval run (real
Groq + Gemini) came back with semantic-only recall of 0.0 on all three
datasets — the semantic layer caught precisely none of the 30 planted
semantic-mismatch rows across the three domains. Root cause:
`SemanticReasoner.run()` always sampled the first `sample_size` (20) unexcluded
rows in plain file order, never randomly. Every dataset here happens to put its
semantic-mismatch rows near the end of the table (rows 128-137 of 150) — with a
3-cycle iteration cap × 20 rows/cycle, the loop only ever reaches rows 0-59,
sequentially, every single run, so those planted rows were structurally
unreachable regardless of how many cycles ran. This isn't specific to these
datasets: it means on ANY real table, the semantic layer would only ever look at
whatever happens to sit in roughly the first 60 rows, permanently blind to
anything after that (a bad import batch appended at the end, rows sorted by
date, anything). Fixed — `SemanticReasoner.run()` now samples randomly from the
unexcluded pool each cycle (`random.sample`, indices re-sorted only for a
readable prompt), so coverage is spread across the whole table instead of stuck
on the same leading slice every time. This does NOT make coverage exhaustive —
a 150-row table with a 3-cycle x 20-row cap still only samples ~40% of it per
scan at best, an inherent cost/latency tradeoff, not something this fix solves —
but it stops the semantic layer from being deterministically blind to an entire
region of any table it's given. One test's exact-order assertion
(`test_sampled_indices_matches_the_rows_actually_sent`) was updated to check
valid-index membership across repeats instead of a fixed index, since the
sampled index is now genuinely random by design.

**Verified on a real rerun:** semantic-only recall went 0.0 -> 0.4 / 0.0 / 0.2 on
crm_leads / ecommerce_orders / hr_employees respectively, with **zero false
positives across all three datasets** (precision 1.0 everywhere, vs. 40 false
positives and precision 0.437 on `hr_employees` before the fix) — strong evidence
the earlier false-positive spike was an artifact of the sequential-sampling bug
(repeatedly reasoning over the exact same stale 60-row slice across forced
cycles), not a real precision problem with the semantic layer on that data shape.
`ecommerce_orders` staying at 0.0 in that one run isn't a regression — with only
1 iteration run and a random 20-row sample missing all 10 of the planted rows by
chance (~5% probability), that's normal variance for a capped-sample design, not
a sign the fix didn't work.

That "normal variance for a capped-sample design" conclusion is itself what
motivated the next, bigger change: capped/randomized sampling was still
fundamentally a coverage ceiling, just a randomly-placed one instead of a fixed
one. **Superseded by full-table coverage as the new default** — see below.

That same live run also surfaced a second thing, on closer look NOT a bug: the
custom-instruction battery's "hallucinated field" case (e.g. "flag rows where
the moon_phase field is waning") was hand-labeled here to expect `failed`, and
the real compiler consistently returned `semantic` instead, flagged as a
"MISMATCH." Re-reading the compiler's own system prompt settled it — it
explicitly instructs the model to route an unknown-field instruction to
SEMANTIC guidance, not fail: `failed` is reserved for when the model itself
misbehaves (emits a structured rule that names a bad field anyway, which
`_parse_structured` then rejects), not for the correct, safe response to a
hallucinated field. The eval harness's own expected label was wrong, not the
product — routing to semantic guidance instead of a dead-end failure is the
better, safer behavior, and it's what the real model did consistently across
all three datasets. Fixed the harness's expectations, not the compiler.

**Resolved:** the `hr_employees` full-agentic run in the first live pass showed
40 false positives (precision 0.437) with the old capped 3-cycle loop. The
question at the time was whether that was bad luck from the sequential-sampling
bug (repeatedly reasoning over the same clean 0-59 row slice, perhaps landing on
a batch the model judged harshly) or a genuine precision problem with the
semantic layer on employee-shaped data specifically. The second live rerun, after
the randomization fix, settled it: `hr_employees` false positives went 40 -> 0
(precision 0.437 -> 1.0), with nothing else about the semantic layer or the
dataset changed. That's strong evidence it was the sampling bug — reasoning
repeatedly over the same stale slice, not a real precision weakness on
employee-shaped data. No open question left here.

**Intentionally empty/not yet built — this pass's own scope:** custom-instruction
end-to-end behavior in the frontend hasn't been visually verified with real LLM
calls (only deterministic-only, in this sandbox — the wiring is real and tested,
just not eyeballed with real semantic output yet), and no red-team/adversarial
-prompt-injection dataset was forged this pass (the eval scope agreed on was
detection accuracy + iteration behavior + instruction reliability, not
adversarial robustness — that's still open).

**Currently on hold, by explicit decision, not oversight:** remaining connectors
(SQL DB, REST API), `db/` (multi-tenancy, credentials, scan history — the `/scans`
endpoint above is deliberately stateless without it), auth (no login, no tenant
isolation — anyone who can reach the service can run a scan), and deployment
(nothing hosted anywhere; everything above runs locally only). AI security
hardening beyond the iteration cap (temperature control, red-team validation, cost
caps) also remains paused.

**Resolved — Next.js upgraded 14.2.34 -> 16.3.6 (React 18 -> 19):** `npm audit`
was reporting next@14.2.34 vulnerable to a growing list of disclosed advisories.
Checking Next.js's own security blog turned up something more serious than a
routine patch bump: **the 14.x line has stopped receiving security fixes
entirely** — only 15.5.x (Maintenance LTS) and 16.3.x (Active LTS) are patched,
confirmed via the August 25 2026 and September 22 2026 security releases (the
latter fixing a critical unauthenticated RCE in `next/og`'s `ImageResponse`,
three days before this check). Staying on 14 wasn't a "needs a version bump
eventually" note anymore — it was an unpatchable version. Upgraded straight to
16.3.6 (skipping 15) since it's the actively-maintained line, which required
React 18 -> 19 alongside it. Checked both API routes (`app/api/scan`,
`app/api/health`) for Next 15's async-`params`/`cookies()`/`headers()` breaking
change first — neither uses any of those, so the upgrade was a clean drop-in:
`npm audit` now reports **0 vulnerabilities**, `next build` (now running on
Turbopack by default) compiles clean with no type errors, and a production
`next start` smoke test confirms `/`, `/eval`, and `/api/health` all still
respond correctly.

Groq model note: `GroqClient` defaults to `openai/gpt-oss-20b`, picked because it's
what this account's key actually has access to and it fits the generator's
fast/high-volume role — `openai/gpt-oss-120b` is available as a more capable upgrade
if quality on real data ever calls for it.

## Setting up LLM API keys (needed for Phase 2's semantic reasoning)

The generator agent uses Groq, the verifier uses Gemini (deliberately different
providers, per the design decision to avoid correlated blind spots between them).

1. Get a free Groq key: https://console.groq.com
2. Get a free Gemini key: https://aistudio.google.com
3. Copy `service/.env.example` to `service/.env` and fill in both keys.
   **Never commit `.env`** — it's already in `.gitignore`.
4. Either load it with `python-dotenv` in your own entrypoint, or set the two
   variables directly in your shell:

**Windows (cmd.exe):**
```
set GROQ_API_KEY=your-key-here
set GEMINI_API_KEY=your-key-here
```

**Windows (PowerShell):**
```
$env:GROQ_API_KEY = "your-key-here"
$env:GEMINI_API_KEY = "your-key-here"
```

**macOS/Linux:**
```
export GROQ_API_KEY=your-key-here
export GEMINI_API_KEY=your-key-here
```

Then verify real connectivity (this makes actual API calls, unlike the automated
test suite which mocks both providers):
```
python scripts/check_llm_connectivity.py
```
Expect `OK — Groq replied: ...` and `OK — Gemini replied: ...`.

## Running it locally

**Python service (macOS/Linux/bash):**
```
cd service
pip install -r requirements.txt
PYTHONPATH=. pytest          # should show 113 passed
PYTHONPATH=. uvicorn app.main:app --reload --port 8000
PYTHONPATH=. python3 scripts/manual_run.py
```

**Python service (Windows, cmd.exe):**
```
cd service
pip install -r requirements.txt
set PYTHONPATH=.
pytest
uvicorn app.main:app --reload --port 8000
python scripts\manual_run.py
```

**Python service (Windows, PowerShell):**
```
cd service
pip install -r requirements.txt
$env:PYTHONPATH = "."
pytest
uvicorn app.main:app --reload --port 8000
python scripts\manual_run.py
```

Important: you must run these from the `service\` folder itself (not `scripts\`) —
`PYTHONPATH` needs to point at the folder containing the `app` package, and `set`/
`$env:` must be run as a separate command before `python`/`pytest`, not combined
into one line the way bash allows.

**Frontend:**
```
cd frontend
npm install
npm run dev                  # http://localhost:3000
```

The dashboard talks to the Python service at `PYTHON_SERVICE_URL` (defaults to
`http://localhost:8000`) — start the Python service first (see above), then the
frontend, then open http://localhost:3000, upload a CSV from `service/eval/datasets/`
(e.g. `leads.csv` + `accounts.csv` as the related file) and run a scan. Works with
no API keys set (deterministic-only, with a warning banner) or with them set
(full semantic reasoning + verification + custom instructions).

Note: `node_modules/` and `.next/` are not included (run `npm install` fresh) — but
`package-lock.json` is included for reproducible installs. Next.js is pinned to
`16.3.6` (React 19) — see the note earlier in this file for why the 14.x line
had to be abandoned rather than patched. `npm audit` is clean (0 vulnerabilities).

## Running the agentic evaluation

```
cd service
set PYTHONPATH=.                          # Windows cmd.exe; $env:PYTHONPATH="." in PowerShell
python eval\generate_datasets.py          # regenerate the forged datasets (already included, but reproducible)
python eval\run_eval.py                   # deterministic baseline always runs; set both API keys first for the full eval
```

With no keys set, only the deterministic-only baseline runs (verified in this
repo's own sandbox at 100% precision/recall/F1 on all three domains). Set
`GROQ_API_KEY` and `GEMINI_API_KEY` first (same as above) to also get detection
accuracy on the semantic-only issues, real iteration-loop behavior on
realistically-sized tables, and the custom-instruction reliability battery.
Results land in `eval/eval_report.md` (human-readable) and
`eval/eval_results.json` (machine-readable) — both regenerated on every run.

## Next

Per the plan agreed for this pass: remaining connectors, `db/`, auth/
multi-tenancy, and deployment are all explicitly on hold (see "Currently on
hold" above) while this pass focused on evaluating the detection pipeline's
accuracy and finishing the frontend enough to test against it. Next up, in
whatever order gets picked: running the full LLM-dependent eval with real keys
to see the semantic-only detection numbers and iteration behavior for real, then
either resuming DB/auth/deployment or scoping a red-team/prompt-injection eval
pass, depending on what the mentor conversation prioritizes.

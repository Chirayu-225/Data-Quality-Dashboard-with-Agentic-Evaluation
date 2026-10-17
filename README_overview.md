# Data Quality Scorecard *(working title)*

A schema-agnostic data-quality platform. Upload a table, get a score and a
row-level explanation of what is wrong with it, from deterministic checks and,
only if you opt in, an AI review that is verified by a second, independent
model.

> The name is not final. Earlier code, docs and commit history use the
> codename "DBCaaS".

---

## 1. Problem statement

Bad data is expensive and usually found late. CRM and operational tables
accumulate duplicates, placeholders, malformed values, stale or contradictory
records and, increasingly, free-text fields that can carry hostile content.
Existing tools in this space (Validity, RingLead, Cloudingo, Insycle) are
mostly rule-driven. They catch what someone already thought to write a rule
for. They struggle with:

1. **Semantic problems.** "Joe's Bakery" filed under industry *Technology* is
   syntactically valid and wrong. Only judgment catches it.
2. **Trust in the score.** A score that silently rewards checks that never ran,
   or that an attacker can talk into a better number, is worse than no score.
3. **Sending sensitive data to AI providers.** Teams will not paste a customer
   table into an LLM without knowing exactly what leaves, and without a way to
   say no.
4. **Cost and reliability at volume.** Reviewing every row with an LLM means
   thousands of calls, provider rate limits, daily quotas and partial failures.
   A scan that crashes at 90% or quietly skips rows is not usable.

The product has to be accurate where rules can't reach, honest about what it
did and did not look at, and safe by default.

## 2. Solution

A two-layer detection pipeline feeding an explainable scorecard.

| Layer | What it does | Needs a key? |
|---|---|---|
| **Deterministic checks** | Completeness, uniqueness (real-world identity, not just identical rows), format validity, type consistency, statistical outliers, referential integrity, prompt-injection detection | No |
| **AI semantic review** *(opt-in)* | A generator model (Groq) reasons over rows and emits structured flags with reasons and confidence. A *different-vendor* verifier (Gemini) confirms, queues for review, or rejects each flag | Yes, plus explicit consent per scan |
| **Scorer** | Confidence-weighted roll-up into five metrics and one overall score. Unevaluated metrics are reported as *not evaluated*, never as 100 | No |

Design commitments that shape everything below:

- **AI review is a true opt-in.** Default off. With it off, no row data leaves
  the process.
- **Masked before it leaves.** Emails and phones (including ones embedded in
  free text) are replaced with stable salted tokens before any LLM prompt.
- **Row data is untrusted.** Prompts frame it as data, a membership check
  rejects flags for rows the model wasn't shown, and rows containing injection
  attempts are quarantined from scoring.
- **Honest coverage.** The result says how many rows were actually reasoned
  over, how many were unreadable, and why a metric wasn't scored.
- **A score is detected defects, not truth.** See `service/docs/SCORING.md`.

## 3. Architecture

```
 Browser (Next.js, thin UI)
   upload CSV(s) ▸ estimate ▸ submit job ▸ poll ▸ cancel ▸ scorecard
        │   same-origin API routes proxy to the service
        ▼
 FastAPI service
 ┌────────────────────────────────────────────────────────────────────┐
 │ /scans/estimate   pre-scan cost / coverage / daily-quota warning   │
 │ /scans, /scans/schema            synchronous                       │
 │ /scans/jobs, /scans/schema/jobs  async; GET (poll+heartbeat),      │
 │                                  DELETE (cancel)                   │
 │                                                                    │
 │  Job store ── bounded workers, 429 queue cap, TTL expiry (410),    │
 │               256-bit ids, auto-cancel of abandoned scans          │
 │                                                                    │
 │  Connector ─▶ CanonicalTable (source-agnostic model)               │
 │      │                                                             │
 │      ▼                                                             │
 │  GeneratorAgent                                                    │
 │   ├ Deterministic check registry                                   │
 │   │   completeness · uniqueness · validity · type consistency ·    │
 │   │   outliers · referential integrity · injection detection       │
 │   ├ RuleCompiler: plain-English instruction → validated rule DSL   │
 │   │   (executed as data, never as code) or semantic guidance       │
 │   ├ PII masking (salted-hash tokens, embedded-text scrub)          │
 │   ├ Semantic reasoning, full-table batches ─────────┐              │
 │   ├ Cross-table reasoning (schema scans)            │              │
 │   └ Verifier (different vendor) ◀───────────────────┘              │
 │                                                                    │
 │  LLM layer: process-wide shared token bucket per (provider, key),  │
 │   reserve-then-correct with real usage, retry/backoff, 413         │
 │   shrink-and-retry, daily-quota circuit breaker, cooperative       │
 │   cancel                                                           │
 │                                                                    │
 │  Scorer ─▶ 5 metrics + overall, versioned (detection, scoring,     │
 │            prompt and confidence versions travel in every response)│
 └────────────────────────────────────────────────────────────────────┘
        │                                  │
   Groq (generator)                  Gemini (verifier)
```

**Repo layout**

```
service/app/
  canonical/    source-agnostic data model, shared type parsers
  connectors/   Connector ABC + CSV connector (SQL / Salesforce deferred)
  checks/       deterministic check registry
  rules/        NL instruction → DSL compiler and safe executor
  agent/        generator, verifier, semantic + cross-table reasoning,
                LLM clients, PII masking, cost estimate
  scoring/      scorer
  domain/       findings, confidence derivation
  jobs.py, cancellation.py, main.py, config.py, version.py
service/eval/   forged datasets with known planted issues + harnesses
service/tests/  395 tests
service/docs/   SCORING.md, DATA_HANDLING.md, DEPLOYMENT.md
frontend/app/   dashboard, schema scan, eval dashboard, components, API proxies
```

**Version stamps.** Every response carries `DETECTION_VERSION` (1.16.0),
`SCORING_VERSION` (1.6.0), the prompt versions and the confidence-derivation
version. A score change can always be traced to a logic change.

## 4. How the project evolved

Each iteration is summarized as what it tried, what it lacked, and what the next
version fixed. Most of the corrections below were found by running the thing
against forged data or a live provider, or by a code review that reproduced the
bug. Few came from reading the code.

### Iteration 0 · Scaffold

- **Tried.** Prove the shape: a canonical internal model, a connector
  interface, a CSV connector, five rule-based checks (null, exact-duplicate,
  format, IQR outlier, foreign key), a FastAPI health check.
- **Lacked.** No AI. Duplicates meant identical rows only, so the same person
  under two IDs was invisible. "Required" columns were inferred from how blank
  the column was, so the emptier a column, the *less* it was penalized.

### Iteration 1 · Agent layer, scoring, dashboard, eval harness

- **Tried.** Groq as generator, Gemini as verifier (two vendors on purpose, to
  avoid shared blind spots), a verifier with confirmed / needs-review / rejected
  outcomes, a rule DSL compiled from plain English, a confidence-weighted
  scorer with field-criticality weighting, a stateless `/scans` endpoint, a real
  Next.js dashboard, and an eval harness with three forged domains (CRM,
  e-commerce, HR) and planted ground truth.
- **Lacked.** Semantic review was one sampled batch, then a capped loop with
  *sequential* sampling. The first live eval showed **semantic recall of 0.0 on
  all three datasets**: the loop only ever reached the first ~60 rows, and the
  planted problems sat later in the file. The same flaw produced 40 false
  positives on one dataset.
- **Improved.** Random sampling lifted recall to 0.4 / 0.0 / 0.2 and cut those
  false positives to 0. It was still a coverage ceiling, so the design moved to
  the next iteration.

### Iteration 2 · Full-table coverage

- **Tried.** Make full-table review the default. Batch through every row, bound
  it only by an explicit `max_semantic_llm_calls` the *caller* sets. The
  product's differentiator is AI judgment, so a layer that only ever sampled
  part of a table undercut it.
- **Lacked.** LLM calls now scale with table size. That exposed provider
  failures the old design never hit: one Gemini hiccup killed verification for
  a whole dataset, transient 503s crashed scans, and a burst of calls hit
  Groq's tokens-per-minute cap.
- **Improved.** Led directly to iteration 3.

### Iteration 3 · Provider resilience

- **Tried.** Three different fixes for three different failures, because
  conflating them was the original mistake: verifier batching (a failed chunk
  degrades only itself), retry with backoff, and pre-emptive pacing with a
  token bucket (retry reacts after a failure, pacing prevents it). Then a
  fourth axis: a single oversized request (413). That fails immediately and
  halves the batch size instead of retrying the same payload three times. The
  eval harness gained a result cache so sweeps stop re-paying for unchanged
  calls.
- **Lacked.** The token bucket belonged to each client object, and a client
  was created per request, so N concurrent scans got N× the budget. Nothing
  understood a *daily* quota. Partial coverage was reported, but progress and
  scoring still treated some unreadable rows as covered.
- **Improved.** Established the rule that call count, token volume and
  throughput rate are independent constraints, and that a fix on one axis says
  nothing about the others.

### Iteration 4 · Score and detection correctness

(`DETECTION 1.2 → 1.9`, `SCORING 1.2 → 1.6`)

- **Tried.** A series of review-driven corrections, each reproduced before it
  was fixed:
  - A metric with nothing applicable now reports **not evaluated**, not 100.
  - The **quarantine model** for prompt-injection rows: visible to reviewers,
    never scored, never sampled into a prompt. Injection patterns split into
    high- and low-confidence tiers so ordinary phrases such as "pre-approved"
    don't blind the model to a large share of a clean table.
  - The accuracy denominator became **rows actually reasoned over**, not total
    rows. A 100-row table with 20 rows reviewed used to report accuracy 100.
  - **Completeness** is now cells-over-cells on configured required columns,
    fixing a units mismatch and the circular nullability.
  - **Duplicates** match on real-world identity (email, or name + company),
    ignore shared role mailboxes (`info@`), flag every group member except the
    first, and carry per-row reasons.
  - **Type consistency**: a 90% majority rule for column types, a single shared
    parser, and traps handled (leading-zero zips, currency, percent). Null
    sentinels ("N/A") are treated as missing, not as a wrong type.
  - Adversarial evals (v1, v2), a schema-scan eval, and **cross-table
    semantic reasoning** with multi-file `/scans/schema`.
- **Lacked.** Sentinel normalization blanked `"NA"` (Namibia) and `"None"` in
  every column. Nothing yet capped cost or controlled what was sent to the AI
  providers.
- **Improved.** Scores became defensible: each number now says what it covers,
  and the ways a score could be silently inflated were closed.

### Iteration 5 · Cost and privacy guardrails

(`DETECTION 1.10 → 1.15`)

- **Tried.**
  - Upload size cap, enforced while streaming, and a row cap on semantic
    review.
  - `/scans/estimate` previews cost and coverage before any scan runs.
  - **AI review became true opt-in** (default off, a consent flag per scan).
  - **PII masking.** First keyed by row index, which made the same email in two
    rows look unrelated and destroyed the duplicate signal. Replaced by
    per-scan salted hash tokens: the same value gets the same token, different
    values never collide, and tokens carry no meaning across scans. Added an
    embedded-text scrub for emails and phones in free text, phone normalization,
    and near-miss rejection (dates, IPs, version numbers, order ids).
  - Content-derived seeds plus `temperature=0` on every call, and a script that
    *measures* determinism rather than asserting it.
  - Async jobs: bounded workers, 429 queue cap, TTL eviction, 256-bit job ids.
  - Next.js moved from 14 to 16 after 14.x stopped receiving security patches.
- **Lacked.** Real behavior under a real provider at real volume was still
  untested. No cancel, no heartbeat, and a job's result lived only in a browser
  tab.
- **Improved.** What leaves the machine, how much it costs, and how long a job
  lives all became explicit, documented and testable.

### Iteration 6 · Live-run hardening

(`DETECTION 1.16.0`, current)

- **Tried.** Triggered by a live 3,000-row scan: 27 of 28 batches reported
  unparseable, then a provider per-day token quota (HTTP 429).
  - A tolerant semantic parser: it recovers JSON wrapped in prose and drops a
    malformed *flag* rather than the whole batch. Garbage still fails safe and
    is reported as a coverage gap.
  - A **process-wide shared rate limiter**, one per provider and key, so
    concurrent jobs split one budget. Estimates are reserved up front and
    corrected from the provider's reported usage. Failed calls are refunded.
  - A **daily-quota circuit breaker** that fails fast instead of retrying a
    call that cannot succeed.
  - **Cooperative cancellation** that reaches every phase, **abandoned-scan
    auto-cancel** through a polling heartbeat, 410 for expired results, and
    usage logging that works even for providers with no bucket.
  - Progress counts only reliably reasoned rows and shows an "unreadable" count.
- **Lacked.** None of this has been run against the live providers since the
  fixes (see §5). My first headroom fix, a flat 2,000-token cushion, would have
  made every call exceed a 7,000-TPM budget. Review caught it, and the
  reserve-then-correct design replaced it.
- **Improved.** A scan can now be cancelled, resumed after a refresh, run next
  to other scans, and stopped by a quota without wasting calls or lying about
  coverage.

### Iteration 7 · Product surface

- **Tried.** The first dashboard was functional but sparse. Now: job id in the
  URL (resume after refresh), a live scan timer anchored to the server's clock,
  Cancel, scan-duration and rows-scanned summary, an AI-review consent panel,
  coverage panel, cost estimate preview, schema-scan and eval views, ambient
  background art, hero preview and how-it-works steps, and a **light/dark
  toggle** that follows the OS until clicked and applies before first paint.
- **Lacked.** Verified only in a headless browser with a fallback font. The
  "not evaluated" note still reads like a warning and uses internal field
  names.
- **Improved.** The scorecard now explains itself: coverage, duration, what was
  skipped and why.

## 5. Current status and known gaps

**Verified:** 395 automated tests pass (providers mocked). The deterministic
layer scores 100% precision and recall on the forged datasets, with zero false
positives. Version stamping, quarantine, masking and cancellation logic are
covered by tests.

**Not yet verified, stated plainly:**

- **No live run since iteration 6's fixes.** The shared limiter, reserve/settle,
  tolerant parser, cancel and heartbeat have only been exercised against mocks.
  A 300-row live test (`testdata/LIVE_TEST_PLAN.md`: two concurrent scans, cancel
  and abandon, shortened TTL) is waiting on the provider's daily quota. Its
  results are a "does each issue type get caught at all" check, not accuracy
  estimates.
- **Determinism is unmeasured.** The seed and `temperature=0` are best-effort
  (both providers say so). The tooling exists; the measurement doesn't.
- **A full 3,000-row AI scan** doesn't fit a free-tier day. It needs a paid
  tier or a capped `max_semantic_llm_calls`.

**Known gaps:**

- Duplicate detection doesn't yet use a first-name + last-name or phone key
  (formatted-phone duplicates are 0/5 deterministically).
- With no `required_columns` set, any blank counts, so sparse optional columns
  (e.g. `notes`) lower the completeness score. The UI has no field for it yet.
- Consistency is *not evaluated* on small or non-numeric tables. That is
  intentional, but the message needs plain wording and a reason.
- No auth, no persistence, nothing deployed. The job id is the only access
  control. Retained results are unmasked in memory until the TTL expires
  (see `DATA_HANDLING.md`).
- Only the CSV connector exists. SQL and Salesforce connectors are deferred.

**Next, in order:** run the live sample; card-number detection (13–19 digits,
Luhn); optional `phonenumbers` / E.164 normalization; determinism measurement
and eval fixtures (leading-zero zips, NA/None, mixed-type, PII in free text);
mixed-type validity scoring; then persistence, auth and deployment.

## 6. Running it

```bash
# service
cd service
pip install -r requirements.txt
PYTHONPATH=. pytest                                   # 395 passed
PYTHONPATH=. uvicorn app.main:app --reload --port 8000

# frontend (a separate terminal; package.json lives in frontend/)
cd frontend
npm install
npm run dev                                           # http://localhost:3000
```

Windows PowerShell: `$env:PYTHONPATH = "."` as its own command before `pytest`
or `uvicorn`.

Optional AI review needs `GROQ_API_KEY` and `GEMINI_API_KEY` (copy
`service/.env.example` to `.env`; never commit it). Without keys, scans run
deterministic-only and say so. Useful knobs: `GROQ_TOKENS_PER_MINUTE`,
`GROQ_TOKENS_PER_DAY`, `SCAN_JOB_TTL_SECONDS`, `SCAN_JOB_MAX_PENDING`,
`SCAN_JOB_ABANDON_SECONDS`. See `service/docs/DEPLOYMENT.md`.

Evaluation: `PYTHONPATH=. python eval/run_eval.py` (deterministic baseline
needs no keys), plus the adversarial and schema harnesses in `service/eval/`.

## 7. Further reading

- `service/docs/SCORING.md`: exact formulas, per-metric semantics, limitations
- `service/docs/DATA_HANDLING.md`: what is sent to whom, masking, retention
- `service/docs/DEPLOYMENT.md`: process-wide limits, job lifecycle, cancel, expiry
- `service/app/version.py`: the full change log, one entry per logic change

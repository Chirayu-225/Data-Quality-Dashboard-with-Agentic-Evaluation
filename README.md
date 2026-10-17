# Data Quality Scorecard *(working title)*

A schema-agnostic data-quality platform. Upload a table, get a score and a
row-level explanation of what is wrong with it. Detection runs in three tiers:
deterministic checks, a local value review that needs no AI and sends nothing
anywhere, and an optional AI review (a generator model plus an independent
verifier) that only runs if you opt in.

> **Status: proof of concept.** Backend feature-complete for the PoC scope,
> dashboard functional but not yet showing the newest fields. No auth, no
> persistence, nothing deployed. See [§9 Known gaps](#9-known-gaps-and-limits).
>
> The name is not final. Earlier code, docs and commit history use the codename
> "DBCaaS".

**Current versions** (every API response carries them):
`DETECTION_VERSION 1.21.0` · `SCORING_VERSION 1.7.0` · `SEMANTIC_PROMPT_VERSION 2.3.0`
· **501 automated tests passing**.

---

## Contents

1. [Problem statement](#1-problem-statement)
2. [Solution](#2-solution)
3. [Architecture](#3-architecture)
4. [Features](#4-features)
5. [Security and privacy model](#5-security-and-privacy-model)
6. [Repository layout](#6-repository-layout)
7. [How the project evolved (iterations 0–9)](#7-how-the-project-evolved)
8. [Evaluation and test results](#8-evaluation-and-test-results)
9. [Known gaps and limits](#9-known-gaps-and-limits)
10. [Roadmap](#10-roadmap)
11. [Running it](#11-running-it)
12. [Configuration reference](#12-configuration-reference)
13. [Further reading](#13-further-reading)

---

## 1. Problem statement

Bad data is expensive and usually found late. CRM and operational tables
accumulate duplicates, placeholders, malformed values, stale or contradictory
records and, increasingly, free-text fields that can carry hostile content.
Existing tools in this space (Validity, RingLead, Cloudingo, Insycle) are mostly
rule-driven. They catch what someone already thought to write a rule for. They
struggle with:

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

The product has to be accurate where rules can't reach, honest about what it did
and did not look at, and safe by default.

## 2. Solution

A three-tier detection pipeline feeding an explainable scorecard.

| Tier | What it does | Needs a key? | Data leaves the machine? |
|---|---|---|---|
| **1. Deterministic checks** | Completeness, uniqueness (real-world identity, not just identical rows), format validity, type consistency, statistical outliers, referential integrity, prompt-injection detection | No | No |
| **2. Local value review** *(default, since iteration 9)* | Reduces each text column to its distinct values and scores every value with independent heuristics (keyboard mashing, placeholder words, record-status markers, role words in a name column, repeated tokens, shape that deviates from the column). Maps findings back to every row holding the value | No | **No** |
| **3. AI review** *(opt-in per scan)* | A generator model (Groq) reasons over masked rows and emits structured flags with reasons and confidence. A *different-vendor* verifier (Gemini) confirms, queues for review, or rejects each flag | Yes, plus explicit consent | Yes, masked (see §5) |
| **Scorer** | Confidence-weighted roll-up into five metrics and one overall score. A metric that wasn't evaluated is reported as *not evaluated*, never as 100 | No | No |

Design commitments that shape everything below:

- **Local first, AI opt-in.** The default scan sends nothing anywhere, and the
  response says so (`data_egress`).
- **Masked before it leaves.** When AI review is on, emails and phones
  (including ones embedded in free text) are replaced with stable salted tokens
  before any prompt.
- **Row data is untrusted.** Prompts frame it as data, a membership check
  rejects flags for rows the model wasn't shown, and rows containing injection
  attempts are quarantined from scoring and from every prompt.
- **Honest coverage.** The result says how many rows were actually reasoned
  over, how many were unreadable, which detectors the accuracy number rests on
  (`accuracy_basis`), and why a metric wasn't scored.
- **A score is detected defects, not truth.** See `service/docs/SCORING.md`.

## 3. Architecture

```
 Browser (Next.js, thin UI)
   upload CSV(s) ▸ estimate ▸ submit job ▸ poll ▸ cancel ▸ scorecard
        │   same-origin API routes proxy to the service
        ▼
 FastAPI service
 ┌─────────────────────────────────────────────────────────────────────┐
 │ /scans/estimate            pre-scan cost / coverage / quota warning │
 │ /scans/instruction-preview what a custom instruction would do       │
 │ /scans, /scans/schema      synchronous                              │
 │ /scans/jobs, /scans/schema/jobs  async; GET (poll+heartbeat+stage   │
 │                            events), DELETE (cancel)                 │
 │                                                                     │
 │  Job store ── bounded workers, 429 queue cap, TTL expiry (410),     │
 │               256-bit ids, auto-cancel of abandoned scans           │
 │                                                                     │
 │  Connector ─▶ CanonicalTable (source-agnostic model)                │
 │      │                                                              │
 │      ▼                                                              │
 │  GeneratorAgent                                                     │
 │   ├ Tier 1  Deterministic check registry                            │
 │   │          completeness · uniqueness · validity · type            │
 │   │          consistency · outliers · referential integrity ·       │
 │   │          injection detection                                    │
 │   ├ Tier 2  Local value review  ── in-process, no network ──┐       │
 │   ├ RuleCompiler: plain-English instruction → validated     │       │
 │   │   rule DSL (run as data, never as code) or guidance     │       │
 │   ├ PII masking (salted-hash tokens, embedded-text scrub)   │       │
 │   ├ Tier 3  Semantic reasoning, full-table batches ─────────┤       │
 │   ├ Cross-table reasoning (schema scans)                    │       │
 │   └ Verifier (different vendor) ◀───────────────────────────┘       │
 │                                                                     │
 │  LLM layer: process-wide shared token bucket per (provider, key),   │
 │   reserve-then-correct with real usage, retry/backoff, 413          │
 │   shrink-and-retry, daily-quota circuit breaker, cooperative cancel │
 │                                                                     │
 │  Scorer ─▶ 5 metrics + overall; versions, data_egress, local_review │
 │            and accuracy_basis travel in every response              │
 └─────────────────────────────────────────────────────────────────────┘
        │ (opt-in only)                    │ (opt-in only)
   Groq (generator)                  Gemini (verifier)
```

**Data flow for a default scan:** CSV → canonical table → deterministic checks →
local value review → scorer → response. No network call is made. With AI review
on, masked rows additionally go to the generator, and flagged rows to the
verifier.

## 4. Features

### Scorecard

Five metrics and an overall score. The overall score is a weighted average but
is capped at 20 points above the worst *evaluated* metric, so one bad area
cannot be averaged away.

| Metric | What it measures |
|---|---|
| Completeness | Cell fill rate on configured required columns (all columns when none configured) |
| Uniqueness | Duplicates by real-world identity: email, name + company, name + phone |
| Validity | Format of email, phone and date fields |
| Consistency | Statistical outliers and type consistency (not evaluated on small or non-numeric tables) |
| Accuracy | Semantic findings from the detectors named in `accuracy_basis` |

Every response also carries:

- `rows_affected` — rows with a real problem. `flagged_rows` is longer because
  `null_check` flags a blank in any column; a row whose only finding is a blank
  in an optional column is marked `counts_as_problem=false`.
- `data_egress` — whether any external provider was used and how many rows went
  to it. With AI off it is always `external_ai_used: false`.
- `accuracy_basis` — `local_rules_only`, `local_rules_plus_ai` or `ai_only`,
  marked not comparable across modes.
- `local_review` — which columns the local review judged, which it skipped and
  why, and how many distinct values it scored.
- `versions` — detection, scoring, prompt and confidence-derivation versions.

### Deterministic checks

Null/blank cells · duplicates (email, first+last or full name + company,
name + phone with digits only; shared role mailboxes like `info@` ignored) ·
format validity (email, phone, date) · type consistency (90% majority rule,
leading-zero zips, currency, percent, null sentinels) · IQR outliers ·
referential integrity (real cross-table lookup, with a fallback mode) ·
prompt-injection detection (high- and low-confidence tiers).

### Local value review (tier 2)

Skips email, phone, date, numeric, identifier and free-text columns (and says
which and why). For each remaining text column it works on distinct values, not
rows. Signals: keyboard mashing (5+ keyboard-row or alphabet characters, or a
4+ character repeat), placeholder/test words, record-status markers ("do not
use", "deleted"), role or department words in a person-name column, repeated
tokens (three or more), digits or symbols where the column is plain text.
Precision is deliberately favoured over recall; see §8 for measured numbers and
the limits.

### Custom instructions and the instruction preview

A plain-English instruction is compiled by `RuleCompiler`, grounded in the
table's real column names. A structured rule (compared against real fields) runs
deterministically over every row with no further LLM call; a semantic
instruction becomes guidance for the reasoning layer; an unknown field routes to
semantic guidance instead of failing silently. `POST /scans/instruction-preview`
shows what the instruction was understood as, before a scan commits: an exact
count for a structured rule (1 call, schema and instruction only), or a small
masked sample for a semantic one (2 calls, labelled a sample, "unknown" when
unreadable).

### Jobs, progress and recording

Asynchronous scans with polling, a heartbeat, cooperative cancel, and TTL expiry.
`GET /scans/jobs/{id}` returns the pipeline stages (`checks`, `instruction`,
`semantic`, `verify`, `score`) with counts, and `?events=true` returns a capped
event timeline (400 events). That timeline is what a recorded run is replayed
from; a replay must be labelled as a recording.

### Frontend

Next.js dashboard: upload, cost estimate preview, AI-review consent panel, live
scan progress with a server-anchored timer and Cancel, score hero, per-metric
meters, coverage panel, flagged-rows table with a **Needs attention** filter
(default when optional-blank-only rows exist), schema scan, eval dashboard, and
a light/dark toggle. *The new fields (`data_egress`, `accuracy_basis`,
`local_review`) are in the API but not displayed yet.*

## 5. Security and privacy model

| Property | Status |
|---|---|
| Nothing leaves the process unless the caller consents per scan | **Built, tested** |
| Zero-egress default scan, with a per-scan ledger (`data_egress`) | **Built, tested** |
| Email and phone masking before any LLM prompt, stable salted tokens, embedded-text scrub | **Built, tested** |
| Prompt-injection quarantine: flagged rows never sampled into a prompt, never scored | **Built, tested** (3 of 3 planted rows caught in live runs) |
| LLM output constrained: structured JSON, flags accepted only for rows actually shown | **Built, tested** |
| Custom rules compile to a restricted DSL, executed as data, never as code | **Built, tested** |
| Per-scan opt-out of additional columns from prompts (`exclude_columns`) | **Built** (API only) |
| Job ids are 256-bit; results expire (TTL); abandoned scans auto-cancel | **Built, tested** |

**Not built, stated plainly:**

- **AI mode still sends whole masked rows.** Names, companies, amounts and notes
  go to the providers as-is. Only email and phone are masked. The planned fix is
  value-level review (isolated values from columns the user marks shareable,
  never rows) and a configurable endpoint (§10).
- **Provider data-retention and training terms on the free tiers have not been
  verified.** Do this before any real customer data is used.
- **Injection detection is pattern-based**: a strong filter, not a guarantee.
- **No auth, no tenant isolation, no persistence.** The job id is the only
  access control. Retained results are unmasked in memory until the TTL expires.
- **Logs:** confirm no row values are printed (not yet audited).

## 6. Repository layout

```
service/
  app/
    canonical/     source-agnostic data model, shared type parsers
    connectors/    Connector ABC + CSV connector (SQL / Salesforce deferred)
    checks/        deterministic check registry
    rules/         NL instruction → DSL compiler and safe executor
    agent/
      generator.py            orchestrates checks, local review, semantic, verify
      local_value_review.py   tier 2: no-LLM value-level review      (iteration 9)
      semantic_reasoning.py   LLM reasoning over masked rows
      verifier.py             independent-vendor verification
      cross_table_reasoning.py
      llm_clients.py          Groq / Gemini wrappers, retry, pacing, quotas
      pii_masking.py          salted-hash masking, embedded-text scrub
      prompt_rules.py         shared prompt fragments (valid-value rule)
      cost_estimate.py
    scoring/       scorer
    domain/        findings, confidence derivation, rows_affected
    instruction_preview.py    "what would this instruction do" endpoint logic
    jobs.py, cancellation.py, main.py, config.py, version.py
  eval/            forged datasets with known planted issues + harnesses
  scripts/         live demos, connectivity checks, eval_local_review.py,
                   value_eval.py, check_iso_rule.py
  tests/           501 tests (conftest.py pins LOCAL_REVIEW=0 for the legacy suite)
  docs/            SCORING.md, DATA_HANDLING.md, DEPLOYMENT.md, COMPARISON.md
frontend/app/      dashboard, schema scan, eval dashboard, components, API proxies
testdata/          leads_300.csv + truth, leads_holdout.* , run records
```

## 7. How the project evolved

Each iteration is summarized as what it tried, what it lacked, and what the next
version fixed. Most corrections were found by running the thing against forged
data or a live provider, or by a review that reproduced the bug. Few came from
reading the code.

### Iteration 0 · Scaffold

- **Tried.** Prove the shape: a canonical internal model, a connector interface,
  a CSV connector, five rule-based checks (null, exact-duplicate, format, IQR
  outlier, foreign key), a FastAPI health check.
- **Lacked.** No AI. Duplicates meant identical rows only, so the same person
  under two IDs was invisible. "Required" columns were inferred from how blank
  the column was, so the emptier a column, the *less* it was penalized.

### Iteration 1 · Agent layer, scoring, dashboard, eval harness

- **Tried.** Groq as generator, Gemini as verifier (two vendors on purpose, to
  avoid shared blind spots), a verifier with confirmed / needs-review / rejected
  outcomes, a rule DSL compiled from plain English, a confidence-weighted scorer
  with field-criticality weighting, a stateless `/scans` endpoint, a real Next.js
  dashboard, and an eval harness with three forged domains (CRM, e-commerce, HR)
  and planted ground truth.
- **Lacked.** Semantic review was one sampled batch, then a capped loop with
  *sequential* sampling. The first live eval showed **semantic recall of 0.0 on
  all three datasets**: the loop only ever reached the first ~60 rows, and the
  planted problems sat later in the file. The same flaw produced 40 false
  positives on one dataset.
- **Improved.** Random sampling lifted recall to 0.4 / 0.0 / 0.2 and cut those
  false positives to 0. It was still a coverage ceiling, so the design moved on.

### Iteration 2 · Full-table coverage

- **Tried.** Make full-table review the default. Batch through every row, bound
  it only by an explicit `max_semantic_llm_calls` the *caller* sets. The
  product's differentiator is AI judgment, so a layer that only ever sampled part
  of a table undercut it.
- **Lacked.** LLM calls now scale with table size. That exposed provider
  failures the old design never hit: one Gemini hiccup killed verification for a
  whole dataset, transient 503s crashed scans, and a burst of calls hit Groq's
  tokens-per-minute cap.
- **Improved.** Led directly to iteration 3.

### Iteration 3 · Provider resilience

- **Tried.** Three different fixes for three different failures, because
  conflating them was the original mistake: verifier batching (a failed chunk
  degrades only itself), retry with backoff, and pre-emptive pacing with a token
  bucket (retry reacts after a failure, pacing prevents it). Then a fourth axis:
  a single oversized request (413) fails immediately and halves the batch size
  instead of retrying the same payload three times. The eval harness gained a
  result cache so sweeps stop re-paying for unchanged calls.
- **Lacked.** The token bucket belonged to each client object, and a client was
  created per request, so N concurrent scans got N× the budget. Nothing
  understood a *daily* quota.
- **Improved.** Established the rule that call count, token volume and throughput
  rate are independent constraints, and a fix on one axis says nothing about the
  others.

### Iteration 4 · Score and detection correctness

(`DETECTION 1.2 → 1.9`, `SCORING 1.2 → 1.6`)

- **Tried.** A series of review-driven corrections, each reproduced before it was
  fixed: a metric with nothing applicable reports **not evaluated**, not 100; the
  **quarantine model** for prompt-injection rows (visible, never scored, never
  sampled into a prompt); the accuracy denominator became **rows actually
  reasoned over**; **completeness** became cells-over-cells on configured
  required columns; **duplicates** match on real-world identity and ignore shared
  role mailboxes; **type consistency** got a 90% majority rule, one shared parser
  and trap handling (leading-zero zips, currency, percent, "N/A" as missing);
  adversarial evals, a schema-scan eval and **cross-table semantic reasoning**.
- **Lacked.** Sentinel normalization blanked `"NA"` (Namibia) and `"None"` in
  every column. Nothing yet capped cost or controlled what went to the providers.
- **Improved.** Scores became defensible: each number says what it covers, and
  the ways a score could be silently inflated were closed.

### Iteration 5 · Cost and privacy guardrails

(`DETECTION 1.10 → 1.15`)

- **Tried.** Upload size cap enforced while streaming, a row cap on semantic
  review, `/scans/estimate`, **AI review as true opt-in** (consent flag per
  scan), **PII masking** (first keyed by row index, which destroyed the duplicate
  signal; replaced by per-scan salted hash tokens that preserve it), an
  embedded-text scrub with near-miss rejection, content-derived seeds plus
  `temperature=0`, async jobs (bounded workers, 429 queue cap, TTL, 256-bit ids),
  and a Next.js 14 → 16 upgrade after 14.x stopped receiving security patches.
- **Lacked.** Real behavior under a real provider at real volume was untested.
  No cancel, no heartbeat, and a job's result lived only in a browser tab.
- **Improved.** What leaves the machine, how much it costs, and how long a job
  lives all became explicit, documented and testable.

### Iteration 6 · Live-run hardening

(`DETECTION 1.16.0`)

- **Tried.** Triggered by a live 3,000-row scan where 27 of 28 batches were
  reported unparseable, then a provider per-day token quota (HTTP 429): a
  tolerant semantic parser (recovers JSON wrapped in prose, drops one malformed
  flag instead of the batch); a **process-wide shared rate limiter** per provider
  and key with reserve-then-correct accounting; a **daily-quota circuit
  breaker**; **cooperative cancellation**; **abandoned-scan auto-cancel** via a
  polling heartbeat; 410 for expired results; progress that counts only reliably
  reasoned rows.
- **Lacked.** None of this had been run against the live providers since the
  fixes. A first headroom fix (a flat 2,000-token cushion) would have made every
  call exceed a 7,000-TPM budget; review caught it.
- **Improved.** A scan can be cancelled, resumed after a refresh, run next to
  other scans, and stopped by a quota without wasting calls or misreporting
  coverage.

### Iteration 7 · Product surface

- **Tried.** The first dashboard was sparse. Now: job id in the URL (resume after
  refresh), live timer anchored to the server clock, Cancel, scan-duration and
  rows-scanned summary, AI-review consent panel, coverage panel, cost estimate
  preview, schema-scan and eval views, how-it-works steps, and a light/dark
  toggle that follows the OS until clicked.
- **Lacked.** Verified only in a headless browser. The "not evaluated" note
  still read like a warning and used internal field names.
- **Improved.** The scorecard explains itself: coverage, duration, what was
  skipped and why.

### Iteration 8 · The live 300-row test campaign

(`DETECTION 1.17 → 1.20`, `SEMANTIC_PROMPT 2.3.0`)

- **Tried.** A 300-row synthetic leads file (`testdata/leads_300.csv`) with 56
  planted problems across eight types and six "trap" sets that must **not** be
  flagged (dates and order ids in notes, embedded PII, the country code `NA`,
  the discount type `None`, leading-zero zips), plus a scoring script
  (`score_vs_truth.py`) that reports recall per type and false flags per trap.
  Then live runs against Groq and Gemini.
- **What the runs found, and the fix for each:**
  - Every Groq batch came back empty: the reasoning model spent its whole output
    budget thinking. Fixed by requesting low reasoning effort (1.17), and by
    treating a still-truncated batch as an oversized request that halves
    (45 → 22 → 11) instead of leaving rows unreviewed (1.19).
  - 37 of 66 AI flags were "email placeholder" on ordinary rows, because the
    models were never told that `EMAIL_<hex>` tokens are the product's own
    masking, and the verifier confirmed them. All prompts now carry a shared
    masking note (1.18).
  - Run 2 scored 97.0 with 297 of 300 rows reviewed, but exposed: semantic
    recall of 4 of 10 planted mismatches; 4 false flags on the valid country
    code `NA` (the model called Namibia invalid); 0 of 5 re-entered contacts
    caught by the deterministic duplicate check; and a Gemini 403, so the
    verifier did not run.
  - Fixes: duplicate detection now reads a first+last name pair as a name and
    gains a **name + phone** key (digits only), taking the phone-format
    duplicates from 0 of 5 to 5 of 5; a prompt rule that a value valid in its own
    standard is not a defect; **rows affected** (display only: 114 flagged rows
    but 46 with a real problem, because 68 were flagged only for a blank in the
    optional `notes` column); and the **instruction preview** endpoint.
- **Run 3, the recording run** (new Gemini key, semantic instruction "flag leads
  whose company name looks fake"): all five stages ran, 7 semantic flags and 7
  confirmed by the verifier, all 7 on genuinely planted rows, **0 false flags on
  the `NA` traps**, 5 of 10 semantic mismatches caught, 11 reasoning passes,
  380 seconds. Saved as `testdata/run3_record.json`.
- **Lacked.** Ten planted values cannot support a percentage. Semantic recall
  stayed at roughly half. The review pass sends whole rows to two external
  providers, and 100,000 rows at 45 per call would be about 2,200 calls.
- **Improved.** Recall, precision on traps and the verifier were each measured on
  a live run rather than assumed.

### Iteration 9 · Local value review, the zero-egress default (this iteration)

(`DETECTION 1.21.0`, `SCORING 1.7.0`)

**Why.** Two review questions about the AI tier: it exposes a company's raw rows
to external providers, and it cannot scale. Both come from the same root cause,
the model reading rows. Almost nothing the semantic layer catches needs a whole
row: "asdfgh" is an implausible company name wherever it appears. Judging
*distinct values* per column removes both problems.

**What changed.**

1. **`app/agent/local_value_review.py`** (new). In-process, no LLM, no network.
   Skips non-text and free-text columns and reports why; reduces columns to
   distinct values; scores values with independent signals; maps findings back to
   every row holding the value; emits the same `SemanticFlag` shape the LLM path
   does, so scoring and display needed no redesign. Quarantined rows are never
   reviewed.
2. **Wiring.** `GeneratorAgent.run(local_review=...)`: with AI off the semantic
   stage runs the local review ("local value review - no data left this
   machine"); with AI on, local findings are merged with the LLM's. Switch off
   with `LOCAL_REVIEW=0`.
3. **Egress ledger.** Every response gets `data_egress` (external AI used?,
   providers, rows sent) and `local_review` (columns reviewed and skipped,
   distinct values scored, values sent externally = 0).
4. **Accuracy labelling.** Accuracy can now be evaluated on an AI-off scan, a
   scoring behavior change: `SCORING_VERSION` 1.6.0 → 1.7.0, `SCORING.md`
   updated, and every response carries `accuracy_basis` naming the detectors
   behind the number and marking it not comparable across modes. (The first cut
   of this iteration bumped only `DETECTION_VERSION`; review caught the missed
   scoring bump.)
5. **Evaluation tooling.** `scripts/eval_local_review.py` (the original dev file)
   and `scripts/value_eval.py` (a fresh, seeded evaluation file each run, with
   per-category recall and 95% Wilson intervals).

**The review that shaped it.** The first version looked perfect and wasn't
trustworthy: it caught 10 of 10 planted values on the development file, but the
word lists had been written after seeing those values. Two rounds of critique
followed, each producing a correction:

| Critique | What was done |
|---|---|
| The 10/10 is not evidence | Built a **held-out** file with a new seed and different values, including ones expected to be missed. Result: **5 of 10**, 0 false flags. That file is now "spent": once misses are seen, it is no longer held out |
| Ten values is too few to quote as a percentage | Built `value_eval.py`: ~110 planted values across 12 categories, grouped into *structural*, *vocabulary* and *needs context*, quoted per group with intervals. Never one blended percentage |
| Rules may misfire on real names from many cultures | Probed with **6,000 rows from 110 Faker locales** (not the author's own lists). Found real false flags: "Liberty", "Haverty", "Doherty", "Huberty" (keyboard-run), "Sales" (role word), "Baró" (an ASCII-only word splitter read it as the placeholder "bar"). Fixed each (below) |
| Accuracy now means different things in different modes | `SCORING_VERSION` bump, `SCORING.md`, `accuracy_basis` field |
| Pre-download models and prove the offline claim | No embedding model is used yet, so nothing to download. The offline check is a documented step (§11) |

**Rule fixes from the multi-locale probe.** Keyboard-row runs need 5+
characters (4 sits inside Liberty/Haverty/Doherty) and alphabet runs 5 (4 hit
"Karstula"); role and placeholder words that are also real surnames (Sales,
Service, Bar, Foo, Sample, Temp, Demo) only count inside multi-word values; two
identical words ("Mei Mei", "Tom Tom") no longer flag, three or more do; a digit
in a company name is tolerated but not in a person's name; word handling is
Unicode-aware, so Polish, Vietnamese, Devanagari, Han and Hangul names are plain
text. After the fixes the 6,000-row probe had one remaining flag: a city name
containing a literal asterisk in Faker's own Nepali data, which is a true
anomaly in the source, not a reviewer false positive.

**Results** (full tables in §8): on a seed the rules never saw, structural
problems 42 of 50, vocabulary problems 30 of 30 (high by construction, not
generalisation), needs-context problems 2 of 30, and **0 false flags in 590 clean
rows** (95% upper bound 0.6%), including occupational surnames and
consonant-cluster names. 100,000 rows load in about 3 s and review in 0.45 s
(synthetic, few distinct values).

**Test runs for this iteration.** 478 tests passing before; 498 after the module
and wiring (the legacy suite runs with `LOCAL_REVIEW=0`, pinned in
`tests/conftest.py`, so it keeps testing the AI-off-is-deterministic contract);
501 after the real-name regression test, the repeated-word test and the
`accuracy_basis` test. Confirmed on both the cloud copy and the user's Windows
machine. An end-to-end check through the real API on `leads_300.csv` with AI off
moved the result from 96.8 / accuracy not evaluated / 46 rows affected to
**97.1 / accuracy 98.3 / 56 rows affected, 10 semantic flags, 0 bytes sent
externally**.

**Lacked.**
- It cannot judge anything needing world knowledge or row context: template
  phrases ("Company Name Here"), plausible-sounding fake companies, single
  characters. Those need the optional AI tier.
- Word lists are generic heuristics, not a model. Company columns containing
  commas or ampersands lower a column's plain-text share and switch off the shape
  checks for that column, a known cause of missed digit strings and symbol
  corruption. Not tuned for, because the seed that showed it is spent.
- AI mode still sends masked whole rows. The frontend does not yet show the
  egress ledger or the accuracy basis.

**Improved.** The default scan is fast, free, deterministic and sends nothing
anywhere, and the claim is checkable from the response and by running it offline.

## 8. Evaluation and test results

All figures are from runs in this repository's history. Provider-dependent
figures are single live runs, not averages.

### Automated tests

| Point in time | Tests passing |
|---|---|
| Iteration 7 | 395 |
| After iteration 8 (live-test fixes) | 478 |
| After iteration 9 (local value review) | **501** (cloud and Windows) |

### Live 300-row campaign (`leads_300.csv`, 56 planted problems)

| Measure | Run 2 (AI on, verifier failed) | Run 3 (AI on, recording) | AI off, before local review | AI off, with local review |
|---|---|---|---|---|
| Overall score | 97.0 | 97.0 | 96.8 | 97.1 |
| Accuracy metric | scored | 97.6 | not evaluated | 98.3 |
| Rows affected | n/a | 51 | 46 | 56 |
| Semantic mismatches caught | 4 of 10 | 5 of 10 | n/a | 10 of 10 on this file (not evidence) |
| False flags on `NA` country code | 4 | **0** | 0 | 0 |
| Verifier outcome | Gemini 403, not run | 7 of 7 confirmed | n/a | n/a |
| Injection rows quarantined | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| Duplicate-by-phone caught | 0 of 5 | 5 of 5 | 5 of 5 | 5 of 5 |
| Scan time | not recorded | 6 min 20 s | 50 ms | well under 1 s |
| Data sent externally | masked rows | masked rows | none | **none** |

Deterministic types (duplicate email, sentinel amounts, garbage amounts, blank
required, outliers, injection) were caught 100% in run 3. No unplanted row was
flagged by anything other than the optional-column blank noise.

Read the semantic rows as "caught at all" checks. Ten planted values cannot
support a rate.

### Local value review

Honesty ladder, from weakest to strongest evidence:

| Test | Result | How to read it |
|---|---|---|
| `leads_300` (development file) | 10 of 10, 0 false flags | **Not evidence.** Word lists were written after seeing these values |
| `leads_holdout` (new seed, different values) | 5 of 10, 0 false flags | Honest but tiny; now spent (misses were inspected) |
| 6,000 multi-locale Faker rows, before fixes | 22 false flags | Found the real-name problems above |
| 6,000 multi-locale Faker rows, after fixes | 1 flag, a genuine anomaly in Faker's own data | |
| `value_eval.py --seed 101` (110 planted, 590 clean) | structural 45/50, vocabulary 30/30, needs-context 3/30, **0 false flags** | Seed 101 was inspected after the run |
| `value_eval.py --seed 202` (fresh seed, run on the user's machine) | structural 42/50 (71–92%), vocabulary 30/30 (89–100%), needs-context 2/30 (2–21%), **0 false flags in 590** (0.0–0.6%) | The number to quote |

Per-category on seed 202: keyboard mash 10/10, repeated characters 10/10, digit
inside a word 10/10, digits only 7/10, symbol corruption 5/10, placeholder words
10/10, status markers 10/10, role in a person-name column 10/10, wrong-field
values 2/5, template phrases 0/10, plausible fake names 0/10, single characters
0/5. With 10 per category, differences between seeds (symbol corruption 8/10 on
seed 101, 5/10 on seed 202) are within noise.

**Rules of use for the evaluation:** a seed whose misses you have looked at is no
longer held out, so use a new seed after any rule change; clean rows come from
Faker across ~110 locales plus a hand-listed set of look-alike traps (occupational
surnames, repeated-part names, consonant-cluster names); quote per-category
numbers, never one blended percentage.

## 9. Known gaps and limits

**Not yet verified, stated plainly:**

- **Determinism of the AI tier is unmeasured.** The seed and `temperature=0` are
  best-effort (both providers say so). The tooling exists; the measurement does
  not.
- **AI-tier numbers are single live runs.** The 4 of 10 → 5 of 10 change in
  semantic recall is within noise at this sample size.
- **A full 3,000-row AI scan does not fit a free-tier day.** It needs a paid tier
  or a capped `max_semantic_llm_calls`.
- **Local review has only been measured on synthetic data**, with Faker as the
  independent source of clean names. No real customer data has been tested.
- **The offline claim** (no internet needed with AI review off) has not yet been
  demonstrated end to end in the UI with Wi-Fi off.

**Known gaps:**

- The UI does not display `data_egress`, `accuracy_basis` or `local_review`. The
  badge "Semantic reasoning: off (no API keys)" is wrong for an AI-off scan on a
  server that has keys (it is off by consent, not by configuration).
- The flagged-row reason "null_check: 96 rows have a blank field" repeats the
  aggregate on every row instead of naming the blank column.
- A planted `amount` flagged by outlier and type checks appears to be weighted at
  2.0 under validity and consistency; not yet investigated.
- With no `required_columns` set, any blank counts for completeness, and the UI
  has no field for it. "Not evaluated" messages still use internal wording.
- Only the CSV connector exists. SQL and Salesforce connectors are deferred.
- No auth, no persistence, nothing deployed.

## 10. Roadmap

**Next, in order:**

1. Frontend: show the egress line ("Data sent externally: none"), the accuracy
   basis, the local-review summary, and correct the AI-off badge.
2. **Replay mode**: play back a recorded run's event timeline, labelled as a
   recording. Re-record after step 3 so the recording matches the architecture
   being presented.
3. **Value-level AI review**: send isolated values (never rows) from columns the
   user marks shareable, to a configurable OpenAI-compatible endpoint, with
   personal columns reviewed locally only. Replaces row-level prompts as the AI
   default.
4. **Embedding pre-ranking** for scale: a small CPU sentence-embedding model
   ranks distinct values by distance from their column so the LLM sees only the
   top candidates, making cost depend on suspicious *values*, not rows. Bundle
   the model with the app so nothing downloads at run time.
5. Evaluation screen (planted vs. detected, recall per type), instruction-preview
   step in the UI, v1-vs-v2 comparison view, trend strip, and confidence shown as
   high/medium/low.
6. Determinism measurement; card-number detection (13–19 digits, Luhn); optional
   E.164 phone normalization; persistence, auth and deployment.

## 11. Running it

**Service (Windows PowerShell; run from the `service` folder):**

```powershell
cd service
pip install -r requirements.txt
python -m pytest -q              # 501 passed
uvicorn app.main:app --reload --port 8000
```

macOS/Linux: `PYTHONPATH=. pytest` and `PYTHONPATH=. uvicorn app.main:app --reload --port 8000`.

**Frontend (separate terminal):**

```
cd frontend
npm install
npm run dev                      # http://localhost:3000
```

**Optional AI review keys:** copy `service/.env.example` to `service/.env` and set
`GROQ_API_KEY` and `GEMINI_API_KEY` (never commit it). Without keys, scans run on
tiers 1 and 2 and say so. Check connectivity with
`python scripts/check_llm_connectivity.py`.

**Check the zero-egress claim (no internet, no keys needed):**

1. Turn off Wi-Fi, start the service and frontend.
2. Upload `testdata/leads_300.csv` with **AI review off**.
3. Expect overall 97.1, accuracy 98.3, rows affected 56, 10 semantic flags.
4. Download the run record and confirm `"external_ai_used": false`.

**Evaluate the local value review (free, no keys):**

```
python scripts\eval_local_review.py ..\testdata\leads_300.csv ..\testdata\leads_300_truth.json
pip install faker
python scripts\value_eval.py --seed <a seed you have not used before>
```

**Other harnesses:** `python eval/run_eval.py` (deterministic baseline needs no
keys), the adversarial and schema evals in `service/eval/`, and
`testdata/score_vs_truth.py <run_record.json> <truth.json>` to score a saved run
against planted truth.

## 12. Configuration reference

| Variable | Default | Effect |
|---|---|---|
| `GROQ_API_KEY`, `GEMINI_API_KEY` | unset | Enable AI review (still needs per-scan consent) |
| `LOCAL_REVIEW` | on | `0` disables the local value review and restores AI-off = deterministic only |
| `GROQ_TOKENS_PER_MINUTE` | 7000 | Pacing budget under the provider cap |
| `GROQ_TOKENS_PER_DAY` | provider cap | Daily-quota circuit breaker |
| `GROQ_REASONING_EFFORT` | `low` | `default` sends no setting |
| `SCAN_JOB_TTL_SECONDS` | see DEPLOYMENT.md | How long a finished result is kept |
| `SCAN_JOB_MAX_PENDING` | see DEPLOYMENT.md | Queue cap before 429 |
| `SCAN_JOB_ABANDON_SECONDS` | see DEPLOYMENT.md | Auto-cancel of a scan nobody is polling |
| `PYTHON_SERVICE_URL` (frontend) | `http://localhost:8000` | Where the dashboard proxies |

Per-scan form fields: `ai_review_consent`, `custom_instruction`,
`required_columns`, `exclude_columns`, `max_semantic_llm_calls`.

## 13. Further reading

- `service/docs/SCORING.md` — exact formulas, per-metric semantics, limitations,
  and what feeds accuracy
- `service/docs/DATA_HANDLING.md` — what is sent to whom, masking, retention
- `service/docs/DEPLOYMENT.md` — process-wide limits, job lifecycle, stage events,
  rows affected, instruction preview, local value review
- `service/app/version.py` — the full change log, one entry per logic change
- `service/docs/COMPARISON.md` — dataset-comparison design
- `DECISIONS_v1_for_critique.md` — proposed design decisions awaiting review
- `README_phase1_archive.md` — the original Phase-0/1 README, kept for history

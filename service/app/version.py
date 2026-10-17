"""
Detection-logic versioning — attached to every scan's response
metadata so a scorecard can be traced back to exactly which prompt
and scoring logic produced it. This answers "why did this dataset
score differently after the evaluation logic changed?" without
needing any persistence: the version strings travel with the
response itself, not a database row.

Deliberately simple for this phase: hand-bumped constants, not a
computed hash of the actual prompt text. A hash would be tamper-proof
but unreadable; a bumped string is human-legible and good enough given
there's one developer and no concurrent-editing risk yet. Bump the
relevant constant whenever its corresponding prompt or scoring logic
changes — that discipline is what keeps this meaningful, so it's
called out explicitly at each prompt's own definition site too.

SEMANTIC_PROMPT_VERSION and CROSS_TABLE_PROMPT_VERSION were both
bumped in this same version as the row-index membership check and the
anti-manipulation prompt hardening (see semantic_reasoning.py and
cross_table_reasoning.py's system prompts) — a real example of exactly
the kind of change this version tracking exists to make visible.
"""

DETECTION_VERSION = "1.21.0"  # 1.21.0: local value review (no LLM) is the
# default semantic tier — distinct values per column are scored in-process
# and findings map back to rows, so an AI-off scan now evaluates ACCURACY
# and can raise semantic flags (set LOCAL_REVIEW=0 for the old behavior).
# 1.20.0: DuplicateCheck reads a first_name +
# last_name pair as a name (before, such tables never ran the name+company
# key) and gains a name+phone key (digits only, country code ignored) — a
# live 300-row test caught 0 of 5 re-entered contacts; SEMANTIC_PROMPT 2.3.0
# adds the "a value valid in its own standard is not a defect" rule after
# the model called Namibia's country code "NA" invalid. 1.2.0: NullCheck now flags a blank in ANY
# column (previously only non-nullable ones) — see
# app/checks/completeness.py's docstring for the reproduced
# nullable-from-data circularity bug this fixes. 1.3.0: DuplicateCheck
# now matches on real-world identity (an `email`-hinted column, or a
# name+company composite) before falling back to the original
# exact-full-row match — see app/checks/uniqueness.py's docstring for
# the reproduced bug (different-ID duplicates were invisible before).
# 1.4.0: the Quarantine Model — a row prompt_injection_check flags was
# pulled OUT of the ordinary flagged_row_indices union entirely (see
# app/agent/generator.py's GeneratorAgent.run) and structurally
# excluded from ever being sampled into a semantic-reasoning batch,
# single-table or cross-table (see _run_semantic_with_iteration's
# quarantined_indices param and main.py's cross-table pair filtering).
# 1.5.0: two corrections to 1.4.0, both from a code review that
# reproduced a concrete gap in it. First, a quarantined row no longer
# disappears from flagged_row_indices/FlaggedRowOut — it stays fully
# visible in the ordinary review list (with a "SECURITY (...)"-prefixed
# reason), since a security problem is never a reason for a row to look
# clean to a human reviewer; it just never contributes to any quality
# SCORE (still true, unchanged) and is still structurally excluded from
# semantic sampling (still true, unchanged). Second,
# app/checks/injection_detection.py's patterns are now split into HIGH
# and LOW confidence tiers — only a HIGH match quarantines a row; a LOW
# match (ordinary business language like "pre-approved", "compliance
# team") is reported (a low-severity SecurityFinding) but does NOT pull
# the row out of semantic review, fixing a reproduced false-positive-
# amplification problem where routine CRM/finance phrasing could
# silently blind semantic coverage over a large share of a clean table.
# 1.6.0: referential_integrity_check's fallback mode (no related table
# available) no longer flags blank FK values itself — a code review
# reproduced that this double-counted the exact same defect NullCheck
# already flags under COMPLETENESS, penalizing one blank cell under two
# separate metrics. Fallback mode still reports the blank count in its
# `detail` string for visibility, and non-blank-but-unverifiable values
# are still never flagged (unchanged) — see
# app/checks/referential_integrity.py's docstring. 1.7.0: DuplicateCheck
# edge cases from a further code review. (a) A shared ROLE mailbox
# local-part (info@, sales@, support@, ...) is now excluded from
# email-key matching entirely, same as a blank — it legitimately serves
# multiple different real contacts at one company, so grouping it was a
# real false-positive risk (see _ROLE_LOCAL_PARTS in uniqueness.py).
# (b) A name+company group of 3+ rows is now reported at LOW confidence
# (CheckResult.row_severity) rather than a certain match — common names
# at large employers make a big group a meaningfully weaker signal than
# a 2-row match. (c) Blank-email exclusion was reviewed and confirmed
# ALREADY correct — verified, not re-implemented as if it had been
# broken. (d) Each duplicate group now flags every member EXCEPT the
# earliest-seen row ("the original") — an explicit correction from the
# same review: flagging the original too doubled the apparent damage of
# one underlying duplication event. (d) changes numeric output on every
# table with a duplicate group: UNIQUENESS's flagged_weight (and
# therefore its score) is lower than before, since one fewer row per
# group now counts against it. 1.8.0: CheckResult gained a new optional
# field, `row_reasons` (row_index -> a specific reason string), used so
# far only by DuplicateCheck. Before this, every flagged row under a
# check showed the SAME aggregate `detail` string regardless of which
# specific signal matched THAT row — confirmed confusing in practice: a
# row caught only by the email signal could show a "name+company: N
# rows..." note that had nothing to do with it, purely because
# DuplicateCheck's `detail` summarizes the whole check's findings, not
# each row's own. main.py's flagged_rows and the internal Finding
# converter (app/domain/finding.py) now both prefer row_reasons when a
# check populates it, falling back to the old aggregate `detail` for
# every other check (unaffected — this field defaults to empty).
# 1.9.0: "probe 5" (type consistency), split into its two real problems
# per the scoring spec (docs/SCORING.md). First, csv_connector.py now
# normalizes a configurable list of null sentinels ("N/A", "NA", "null",
# "-", etc.) to blank AT INGEST — those mean "missing," not "wrong
# type," so they're COMPLETENESS's problem, and letting them through
# unnormalized was what let a 40%-"N/A" integer column score 100
# before. Second, csv_connector.py's own _infer_type now looks at a
# column's WHOLE set of non-blank values (not just the first 50) and
# uses a 90% MAJORITY threshold instead of requiring every value to
# parse — a column that's 95% clean integers and 5% genuinely bad
# values ("abc") is still typed INTEGER, with the 5% left for a new
# check, TypeConsistencyCheck (app/checks/type_consistency.py), to flag
# under VALIDITY. A column below the 90% threshold stays STRING, and
# TypeConsistencyCheck reports it as one column-level note (zero rows
# flagged) rather than manufacturing per-row noise on a genuinely mixed
# column. `app/canonical/type_conformity.py` is the single shared
# parser module behind all of this — csv_connector.py's inference,
# TypeConsistencyCheck's row-level conformance check, and OutlierCheck
# (app/checks/consistency.py, which previously silently skipped any
# value a bare float(v) couldn't parse) all now agree on what counts as
# an integer, a float, or a boolean, including the specific
# false-positive traps a naive parse walks into: a leading-zero value
# ("02134", a zip code) is kept OUT of integer classification since it
# doesn't round-trip through int() unchanged; currency symbols,
# thousands separators, and a trailing "%" are stripped before a
# numeric parse; and "yes"/"no"/"y"/"n" are recognized as boolean
# alongside "true"/"false" — but "1"/"0" deliberately are NOT, since
# those are genuinely ambiguous with an ordinary integer flag column.
# 1.10.0: two cost/safety caps, not a scoring or check-logic change —
# neither one does anything to a scan that was already under both
# limits. First, a hard per-upload size cap (app/config.py's
# max_upload_bytes, default 20 MB), enforced by main.py's
# _read_upload_with_size_cap WHILE STREAMING the upload rather than
# after buffering the whole thing into memory — a file over the cap is
# rejected with a 413 before it's ever fully read. Second, a per-table
# ROW cap specifically on semantic reasoning (max_semantic_reasoning_rows,
# default 5000, separate from the existing max_semantic_llm_calls budget
# cap): a table over this limit runs deterministic checks only unless
# the caller passes allow_large_semantic_scan=true (see main.py's
# _llm_for_table), since full-table LLM coverage makes cost scale with
# file size and this keeps that scaling bounded by default rather than
# unbounded. /scans/estimate's cost_estimate.py mirrors the same gate so
# the pre-scan preview never promises coverage a real scan wouldn't
# actually deliver. The cross-table semantic pass (schema scans) is
# deliberately NOT gated by this — it's bounded by FK-pair count, not
# row count, so it isn't the same cost-scaling concern.
# 1.11.0: two PII-exposure mitigations (item 5), neither a scoring or
# check-logic change. First, AI review is now a TRUE opt-in, default
# OFF: /scans and /scans/schema both gained ai_review_consent (Form
# field, default false) and _configure_llms now refuses to construct
# an LLM client at all without it, even when GROQ_API_KEY/GEMINI_API_KEY
# are configured — a scan that doesn't pass it gets deterministic
# checks only, with a warning distinct from "keys not configured" (see
# _configure_llms's docstring). Second, app/agent/pii_masking.py masks
# every email/phone-hinted column (plus any caller-named
# `exclude_columns`) with a stable per-row placeholder (EMAIL_7,
# PHONE_7, ...) before a row ever reaches a semantic-reasoning,
# verification, or cross-table-reasoning prompt — unconditionally,
# independent of consent, since consent governs whether data leaves at
# all and masking governs what's in it when it does. Deterministic
# checks and the final report both still see/show the REAL values;
# only the LLM-facing prompt copy is masked (see generator.py's
# GeneratorAgent.run and main.py's cross-table pass, both of which
# build a masked copy specifically for the LLM calls and keep the real
# table for everything else).
# 1.12.0: live-determinism mitigation (item 6) — not a scoring or
# check-logic change either. app/agent/llm_clients.py now pins a
# content-derived `seed` (_deterministic_seed: a pure hash of the exact
# system+user prompt text) on every Groq and Gemini call, alongside the
# existing temperature=0. This is explicitly a best-effort mitigation,
# not a guarantee — both providers' own documentation says so (Groq's
# API reference states "Determinism is not guaranteed" even with a
# fixed seed; Gemini's own community forum has reports of non-identical
# output for an identical prompt with a fixed seed and temperature=0 on
# at least one model — see docs/SCORING.md's "Live determinism" entry
# for the sourced quotes, previously an UNSOURCED assumption in this
# same document). scripts/check_live_determinism.py is the new,
# empirical companion: it sends the same prompt N times to each real
# provider and reports whether the replies actually came back
# byte-identical, rather than this project asserting a number nobody
# measured. Row SAMPLING (which rows the model is shown) was already
# deterministic before this version (semantic_reasoning.py's
# _content_seed, from DETECTION_VERSION 1.x's original sampling fix)
# and is unaffected by this change — this version is specifically about
# the model CALL's own reproducibility, the other half of the same
# original bug report.
# 1.13.0: a product-review-reproduced correction to 1.11.0's masking
# design, not a scoring or check-logic change. 1.11.0's placeholder was
# keyed by ROW INDEX (EMAIL_7 for whatever row 7's value happened to
# be) — a real defect: the exact same email in two different rows
# produced two different, uncorrelatable tokens, destroying the
# semantic layer's ability to notice "these two records share a
# contact," one of the strongest duplicate/fraud signals in this kind
# of data. Every placeholder is now sha256(per-scan salt + lowercased
# real value), truncated — the SAME value anywhere in one scan (any
# column, any row, either side of a cross-table pair) always produces
# the SAME token, while a different value never collides with it; a
# fresh salt per scan request means a token carries no meaning across
# two different scans. Second, masking gained an EMBEDDED layer: every
# string column not already structurally masked (by semantic_hint or
# exclude_columns) is now also regex-scrubbed for email/phone-shaped
# substrings, closing the gap where a column named `mobile`/`tel`/
# `contact` (missed by the name-based hint heuristic) or a free-text
# `notes` field carrying an embedded email/phone went out in the clear
# under 1.11.0. See app/agent/pii_masking.py's module docstring and
# docs/DATA_HANDLING.md for the full design, including the deliberate
# choice to leave names unmasked by default.
# 1.14.0: a product-review-reproduced correction to 1.9.0's null-sentinel
# normalization, not a scoring-formula change (though it does change which
# cells COMPLETENESS counts as missing, in both directions, on affected
# tables). 1.9.0 blanked a sentinel string ("N/A", "NA", "null", "-", ...)
# in EVERY column unconditionally — a real defect: "NA" is Namibia's own
# ISO country code, "None" is a legitimate answer in an allergies or
# discount-type field, and "-" is a meaningful placeholder in plenty of
# real schemas, so a clean categorical column could suddenly show
# completeness failures that had nothing to do with missing data.
# csv_connector.py now decides eligibility PER COLUMN, from that column's
# own non-sentinel raw values: only a column that LEANS numeric or date
# (app/canonical/type_conformity.py's new `looks_like_date`) gets sentinel
# normalization at all; every other column keeps a sentinel string as a
# literal value, same as any other. Second, a normalized cell's ORIGINAL
# text is no longer silently lost — CanonicalTable gained `raw_overrides`
# (row_index -> {column: original_string}) and a `display_row(idx)` helper,
# and main.py's flagged_rows/security_findings now build `row_data` from
# `display_row` instead of `rows` directly, so a caller sees "N/A" in a
# flagged cell instead of an unexplained blank one. `rows` itself still
# holds the blank — every check and the scorer are unaffected and keep
# seeing the normalized value, exactly as before; only the human-facing
# view changed.
# 1.19.0: even at low reasoning effort one 45-row Groq batch still spent its
# whole 2,048-token output budget thinking (finish_reason=length). That is
# now raised as LLMRequestTooLargeError (never retried), so SemanticReasoner
# halves the batch (45 -> 22 -> 11 ...) instead of leaving 45 rows unreviewed;
# the rule compiler treats it as a failed compile. Cost: later batches of that
# scan are smaller, so it makes more calls.
# 1.18.0: second finding of that run: 37 of 66 AI flags were "email
# placeholder" on ordinary rows, because the models were never told that
# EMAIL_/PHONE_ tokens are our privacy masking; the verifier saw the same
# tokens and confirmed them. All four prompts (semantic, verifier,
# cross-table, cross-table verifier) now carry a shared masking note
# (pii_masking.MASKING_NOTE); SEMANTIC and CROSS_TABLE prompt versions 2.2.0.
# 1.17.0: found by the first live 300-row run: every Groq batch came
# back empty. openai/gpt-oss-20b is a reasoning model; with default
# effort it spent all 2,048 output tokens thinking (finish_reason=length)
# and wrote no answer. The Groq client now requests reasoning_effort=low
# for gpt-oss models (GROQ_REASONING_EFFORT overrides; "default" turns it
# off), which finished in ~1,200 tokens on a 45-row batch. This changes
# what the model sees and says, so recall must be re-measured.
# 1.16.0: found by a live 3,000-row scan where 27 of 28 batches were
# reported unparseable. The semantic response parser now recovers JSON
# wrapped in prose and drops only a malformed individual flag instead
# of the whole batch (garbage still fails safe AND is reported as a
# coverage gap); unparseable snippets are logged. A provider
# per-DAY token quota is no longer retried (it cannot succeed), and
# /scans/estimate warns when projected tokens exceed the daily cap.
# Scoring formula unchanged (SCORING_VERSION stays).
# 1.15.0: masking hardening from a product review, no scoring change.
# Phone numbers are normalized before hashing (digits only, extension
# dropped, last 10 digits) so differently formatted copies of one number
# share a token, and the embedded phone scrub now requires a plausible
# 7-15 digit phone shape and rejects dates (including inside free text),
# IP addresses, dotted version numbers and order/invoice ids — see
# app/agent/pii_masking.py and the near-miss corpus in
# tests/test_pii_masking.py. Also async-job hardening (bounded worker
# pool, 429 queue cap, TTL eviction, 256-bit job ids; app/jobs.py) and a
# /scans/schema/jobs variant — infrastructure only.
SEMANTIC_PROMPT_VERSION = "2.3.0"  # 2.0.0: anti-manipulation hardening +
# membership check. 2.1.0: a code-review-reproduced gap in 2.0.0's own
# hardening — the manipulation-signal paragraph told the LLM to flag
# "pre-approved", "already reviewed", "verified by", and "compliance
# team"/"compliance notice" with high confidence, the exact same
# LOW-tier phrases DETECTION_VERSION 1.5.0's injection_detection.py
# split out as ordinary business language that should NOT quarantine a
# row. The prompt now explicitly carves those phrases out as not a
# manipulation signal, so a real model run doesn't silently undo that
# tiering — see semantic_reasoning.py's _SYSTEM_PROMPT.
CROSS_TABLE_PROMPT_VERSION = "2.2.0"  # same 2.1.0 fix, cross-table variant
# (see cross_table_reasoning.py's _SYSTEM_PROMPT); _VERIFIER_SYSTEM_PROMPT
# was already correct and untouched.
SCORING_VERSION = "1.7.0"  # 1.7.0: ACCURACY can now be evaluated on an AI-off
# scan, because the local value review (no LLM) feeds the same semantic-flag
# path. The formula is unchanged, but what feeds the metric is not: an
# AI-off accuracy score and an AI-on one rest on different detectors and
# are not comparable, so every response carries `accuracy_basis` naming
# the detectors behind it. Local findings have no verifier, so they count
# at the unverified weight. 1.2.0: per-metric evaluated/N/A tracking +
# the overall-score severity cap (see app/scoring/scorer.py's module
# docstring, "Bug 1"/"Bug 2") — unlike 1.1.0, this DOES change numeric
# output: a metric with nothing applicable now reports score=None
# instead of 100, and overall_score can no longer be diluted arbitrarily
# far above the worst evaluated metric. 1.3.0: the prompt-injection
# backstop (prompt_injection_check) no longer contributes to ACCURACY
# at all — a quarantined row is a security event, not a quality
# Finding, so it's excluded from both Finding conversion and
# evaluated-metric tracking (see scorer.py's module docstring). This
# also changes numeric output: a table whose only ACCURACY signal was
# an injection flag now reports ACCURACY as evaluated=False instead of
# a score dragged down by a security match. 1.4.0: Bug 3 — ACCURACY's
# own denominator now excludes quarantined rows
# (total_rows - quarantined_row_count) instead of the whole table, so a
# quarantined row is no longer silently credited as "evaluated and
# clean" in the percentage math just because it contributes zero
# flagged_weight. This LOWERS ACCURACY's apparent score on any table
# with quarantined rows compared to the unfixed 1.3.0 formula, since
# the unfixed version was inflating it by padding the denominator with
# rows that were never actually evaluated — a real correction, not a
# regression, per the module docstring's own worked example (10 rows,
# 4 quarantined, 1 unverified confirmed flag: 95.0 under the bug vs.
# the correct 91.7). 1.5.0: 1.4.0's own denominator was still too
# generous — total_rows - quarantined_row_count silently credited rows
# skipped by an opt-in LLM-call budget, a provider outage mid-scan, or
# an unparseable batch response as "evaluated and clean" even though
# the semantic layer never actually reasoned over them. ACCURACY's
# denominator is now scan_result.semantic_rows_considered — the real
# count of rows a genuinely parsed batch covered (see
# GeneratorAgent._run_semantic_with_iteration's 4th return value and
# ScanResult.semantic_rows_considered). Reproduced concretely: a
# 100-row table with max_semantic_llm_calls=1 at batch size 20 used to
# report ACCURACY=100.0 (denominator 100, nothing flagged) after only
# 20 of 100 rows were ever reasoned over; the fixed denominator is 20.
# This also makes accuracy_total_rows == 0 (nothing reliably reasoned
# at all) the explicit trigger for ACCURACY reading as N/A, rather than
# relying on semantic_iterations > 0 as a coincidental proxy. 1.6.0:
# Bug 4 — config-driven requiredness with the units fix. COMPLETENESS's
# field-criticality weighting was STILL circular even after 1.2.0-1.5.0
# and NullCheck's own fix: it keyed off ColumnSchema.nullable, and for
# the CSV connector `nullable` is itself inferred from the same
# column's blank ratio — reproduced directly (40%-blank vs 60%-blank
# columns scored completeness 20.0 vs 40.0, worse data scoring higher,
# the same inversion one layer up). It was also a genuine UNITS
# mismatch: a weighted numerator (2.0/1.0 per row) over an unweighted
# row-count denominator. COMPLETENESS is now computed as the fraction
# of CONFIGURED required cells that are filled — both sides of the
# fraction in the same (cell-count) units — via a caller-supplied
# `required_columns` list (see compute_scorecard's parameter and
# app/main.py's `required_columns` request field). Non-required columns
# get an informational fill-rate report (ScoreCard.non_required_fill_rates)
# instead of a score penalty, addressing "flagging every blank
# overcorrects" without reintroducing the blank-ratio circularity to
# fix it. With no required_columns configured (the common case so far),
# completeness falls back to an unweighted overall fill rate rather
# than N/A (ScoreCard.completeness_mode == "no_required_fields_configured"),
# so the metric doesn't disappear for most scans. Every other metric
# (VALIDITY, CONSISTENCY, ACCURACY) is unchanged — their field-
# criticality weighting still exists and still keys off `nullable`,
# since the reproduced circularity and units mismatch were both
# specific to COMPLETENESS.
CONFIDENCE_DERIVATION_VERSION = "1.0.0"  # app/domain/confidence.py's base
# rates + confirmed-boost/technical-failure-penalty constants. Bump
# this whenever any of those numbers change — it's the one piece of
# scan-response metadata that ISN'T also covered by DETECTION_VERSION,
# since derive_confidence() is called after Finding conversion, not
# part of it, and a change to its constants alone wouldn't otherwise
# be visible in the versions a scan response carries.

COMPARISON_VERSION = "0.1.0"  # app/comparison: two-version comparison and
# column profiler (schema diff with rename proposals, key-matched row diff,
# null-rate and PSI drift, format-normalized content hash). Bump whenever the
# normalization rules, rename heuristic, drift binning or default thresholds
# change, since any of those can change a comparison result.

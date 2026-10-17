# Scoring spec

`SCORING_VERSION: 1.6.0` — this document describes exactly that version of the
scoring logic in `app/scoring/scorer.py`. If that constant has bumped since,
treat this document as stale until it's updated to match (see "Keeping this in
sync" at the bottom).

## What the score means — and what it doesn't

The score is a measure of **detected defects**, not of absolute truth. A table
can score 100 on a metric and still contain real problems that no check, no
semantic pass, and no cross-table comparison happened to catch. Conversely, a
genuinely fine value can occasionally get flagged (an outlier check doesn't
know a large one-off deal is legitimate; a semantic pass can misjudge).

Treat the score as: *"of the defect types this system knows how to look for,
here's how much of that we found."* It is not a certification that the data is
correct, and it should never be presented to an end user as one. The known
limitations section below lists specific blind spots this version has.

## Per-metric formulas

There are five metrics (`app/checks/base.py::MetricCategory`): **completeness**,
**uniqueness**, **validity**, **consistency**, **accuracy**. Each one is either
`evaluated` (something actually ran) with a `score` from 0–100, or not evaluated
at all, in which case `score` is `None` — never a default 100. A caller must
check `evaluated` before reading `score`; treating a missing score as 100 is
exactly the bug this version's Bug 1 fix was written to prevent.

### Completeness

**Unit:** fraction of cells, not rows.

**Formula, with `required_columns` configured (the "configured" mode):**

```
completeness = 100 * (1 - blank_required_cells / total_required_cells)
```

Only the columns named in `required_columns` count. Each row contributes one
"required cell" per named column that exists on the table; a blank one is a
defect. Columns that exist on the table but aren't named in `required_columns`
get an informational-only fill rate (`non_required_fill_rates`) — they never
affect the score.

**Formula, with no `required_columns` configured (the fallback mode,
`completeness_mode == "no_required_fields_configured"`):**

```
completeness = 100 * (1 - blank_cells / total_cells)
```

An unweighted fill rate across every column on the table, required or not.
This is the common case today, since there's no persisted schema/config layer
yet for a caller to set `required_columns` ahead of a scan.

**Consequence worth knowing before a demo:** in fallback mode, a table with
legitimately sparse *optional* columns — a `fax_number` or `middle_name` field
that's blank for most rows by design, not by defect — will score lower on
completeness than it "should," purely because nothing has told the scorer
those columns don't need to be filled. This is expected behavior given the
current fallback, not a bug; it goes away once `required_columns` is
configured to name only what's actually required.

**N/A when:** never, in either mode — completeness always has a denominator
(total cells is 0 only for a zero-column table, which also has zero rows, and
that degenerate case reports 100.0 by convention, same as "nothing evaluated"
elsewhere in the scorer).

**Worked example:** a 10-row table with `email` and `account_id` configured
as `required_columns`. `email` is blank in 1 row, `account_id` in 2 rows. Total
required cells = 20 (10 rows × 2 columns); blank required cells = 3.
`completeness = 100 * (1 - 3/20) = 100 * 0.85 = 85.0`. A `fax_number` column on
the same table that's blank in 7 of 10 rows contributes nothing to that 85.0 —
it shows up only in `non_required_fill_rates["fax_number"] = 30.0`.

### Uniqueness

**Unit:** rows, confidence-weighted.

```
uniqueness = 100 * (1 - flagged_weight / total_rows)
```

`flagged_weight` is the sum, over every row `DuplicateCheck` flagged, of that
row's weight (1.0 for a confirmed match via an email-hinted column or a
name+company composite (the name may be one column or a first_name + last_name
pair), or a name+phone composite (digits only, a leading country code ignored);
1.0 for an exact full-row match; a lower weight is not
currently used for uniqueness specifically, though the general field-
criticality weighting described under Validity applies if a check populates
`flagged_fields`). A duplicate group's earliest-seen row ("the original") is
never itself flagged — only the later members of the group count as the
defect, so an N-row duplicate group costs N-1, not N.

**N/A when:** `DuplicateCheck` (or any other check under this metric) never
ran this scan.

### Validity

**Unit:** rows, confidence-weighted by field criticality.

```
validity = 100 * (1 - flagged_weight / total_rows)
```

Any row a validity-category check flags contributes its weight to
`flagged_weight`: 2.0 if the check attributed the flag to a required
(non-nullable) field, 1.0 if to an optional field or to no specific field at
all. A row flagged by more than one validity check, or on more than one field,
counts once at its single highest weight (`max()`, never summed) — flagging
the same row twice under the same metric doesn't double the damage.

**N/A when:** no validity-category check ran this scan.

### Consistency

Same formula and weighting convention as Validity, scoped to
consistency-category checks (currently just `OutlierCheck`, which only applies
when the table has at least one numeric column).

**N/A when:** no consistency-category check ran — in practice, whenever the
table has zero numeric columns, since that's the only check in this category
today and it declines to run (`applies_to() == False`) rather than reporting a
false 100.

### Accuracy

**Unit:** rows, confidence-weighted — but with a denominator that is
*deliberately not* the whole table.

```
accuracy = 100 * (1 - flagged_weight / semantic_rows_considered)
```

`flagged_weight` sums confirmed (1.0), needs-review (0.5), and unverified (0.5)
semantic flags, same confidence convention as the deterministic metrics.
`semantic_rows_considered` is the count of rows the semantic layer *reliably
reasoned over this scan* — not the table's row count. It specifically
excludes: rows that were quarantined (flagged by `prompt_injection_check` at
HIGH confidence) and therefore never sampled at all; rows an opt-in LLM-call
budget or a provider outage never got to before coverage stopped early; and
rows whose batch response came back unparseable.

**What feeds accuracy (SCORING_VERSION 1.7.0).** Accuracy is built from
"semantic" flags, and there are now two sources of them: the local value
review (rules over distinct values, no LLM, always available) and, with AI
review on, the LLM generator plus verifier. Every scan response carries
`accuracy_basis` saying which ran (`local_rules_only`,
`local_rules_plus_ai`, `ai_only`) so a score from one mode is never read as
comparable to another. Local findings have no verifier, so they count at the
unverified weight (0.5). Local review sees one value at a time and cannot
judge anything needing world knowledge or row context, so an AI-off accuracy
number is a statement about what those rules could see, not about the whole
table's correctness.

**N/A when:** `semantic_rows_considered == 0` — nothing was reliably reasoned
over, whether because semantic reasoning was off, every sampled row was
quarantined, or every batch failed to parse.

**Worked example (the actual reproduced bug this denominator fixes):** a
100-row table scanned with `max_semantic_llm_calls=1` at batch size 20. Only
20 of 100 rows are ever sampled by the semantic layer; say the batch finds
nothing. The *wrong* formula (`total_rows` as denominator) reports accuracy as
a clean 100.0, silently crediting 80 rows that were never looked at as
"reasoned over and clean." The correct formula uses `semantic_rows_considered
= 20` as the denominator — with nothing flagged in that batch, accuracy is
still 100.0 in this particular example, but the field now correctly reflects
*"100% of the 20 rows we actually looked at,"* not *"100% of all 100 rows,"*
and a caller showing the raw `semantic_rows_considered` value alongside the
score (the roadmap's open UI gap) is what makes that distinction visible
instead of implicit.

## Overall score

```
overall_score = weighted_average(evaluated metric scores)
overall_score = min(overall_score, min(evaluated metric scores) + 20.0)
```

Metrics that aren't `evaluated` are excluded entirely from both the average
and the cap — never treated as a 100 and never treated as a 0. If literally
nothing was evaluated (degenerate case, e.g. a zero-row table), `overall_score`
defaults to 100.0 by convention, since there's no honest number to compute
either way.

**The severity cap (`20.0`), and why it's exactly that number:** without a
cap, a single severely broken metric gets diluted away by four healthy ones —
reproduced directly with a table that scored `completeness = 20.0` in
isolation (correctly alarming) but an `overall_score` of `84.0` once averaged
with four untouched, trivially-100 metrics. The cap pulls `overall_score` down
toward the single worst evaluated metric, by at most the allowance, so one bad
dimension can't be smoothed away by metrics that are clean only because
nothing touched them.

Honestly: **20.0 was picked by judgment, not derived from data.** The
reasoning behind the number is "generous enough that a genuinely well-rounded
scan (every metric in the 80s–90s) is never touched by it, while still
preventing the specific 20→84 dilution case above from recurring." It has not
been tuned against a labeled dataset or validated as the "correct" allowance —
treat it as a tunable parameter, not a principled constant, until it's
revisited with real evidence one way or the other.

## The five original scoring probes — before / after

| # | Probe | Before | After | Status |
|---|---|---|---|---|
| 1 | A metric with nothing applicable (e.g. consistency on an all-text table) | Silently scored 100 — "never ran" and "ran, found nothing" were indistinguishable | Reports `evaluated=False`, `score=None`; excluded from the overall average entirely | **Fixed** |
| 2 | A table with one severely broken metric (completeness=20.0) averaged against four healthy ones | `overall_score = 84.0` — the one real problem diluted away | `overall_score` capped at `min(evaluated) + 20.0` | **Fixed** |
| 3 | A 100-row table, semantic coverage capped to 20 rows by budget/outage/parse failure | `accuracy = 100.0` — the 80 uncovered rows silently credited as "reasoned over and clean" | `accuracy` denominator is `semantic_rows_considered` (20, in this example), not `total_rows` | **Fixed** |
| 4 | Two otherwise-identical tables, 40% vs 60% blank on the same required-looking column | `completeness` scored 20.0 and 40.0 respectively — *worse* data scored *higher*, because requiredness was inferred from the same blank ratio being graded | `completeness` is the fraction of cells in a caller-configured `required_columns` list that are filled — cell-level, not derived from the data's own blank ratio | **Fixed** |
| 5 | An integer column that's 40% the string `"N/A"` | Scores 100 — type inference samples only the column's first 50 values, and nothing downstream checks a row's value against its column's inferred type | `"N/A"` normalizes to blank at ingest, but only in a column that LEANS numeric or date (a null sentinel, caught by completeness, not a type violation — gated per-column as of `DETECTION_VERSION` 1.14.0, see below); type inference now looks at the whole column with a 90% majority threshold; a genuinely non-conforming value (e.g. `"abc"`) is flagged under validity by `TypeConsistencyCheck` (`DETECTION_VERSION` 1.9.0) | **Fixed** |

## Known limitations

- **Type consistency (probe 5) — fixed in `DETECTION_VERSION` 1.9.0, but
  narrowly; sentinel gating corrected in 1.14.0.** `"N/A"`/`"null"`/`"-"`
  and similar sentinels normalize to blank at ingest (completeness's job),
  and a genuinely non-conforming value (`"abc"` in an integer column) is
  flagged under validity by `TypeConsistencyCheck`. 1.9.0 did this
  UNCONDITIONALLY, in every column — a real defect a product review
  reproduced: `"NA"` is Namibia's own ISO country code, `"None"` is a
  legitimate answer in an allergies or discount-type field, and `"-"` is a
  meaningful placeholder in plenty of real schemas, so a clean categorical
  column could show a completeness failure that had nothing to do with
  missing data. As of 1.14.0, `csv_connector.py` only normalizes a
  sentinel in a column that LEANS numeric or date (majority of its own
  non-sentinel values parse as one or the other); every other column keeps
  the sentinel text as a literal value. A normalized cell's original text
  is also no longer lost — `CanonicalTable.raw_overrides` /
  `display_row()` keep it available for a human-facing view
  (`flagged_rows`/`security_findings`), while every check and the scorer
  still see the normalized blank. Known remaining edges: the null-sentinel
  list, the 90% majority threshold, and the 50%-lean gating threshold are
  all fixed constants, not yet configurable per dataset; a column that's a
  genuine majority-numeric mix with a meaningful MINORITY of non-conforming
  values (not just a sprinkle) still only gets a column-level note with no
  row-level validity impact — see the mixed-type scoring gap noted
  separately below, not yet fixed; and this only runs for the CSV connector
  today, since that's the only connector with a type-inference step to pin
  down.
- **Mixed-type STRING columns still don't contribute to validity at the row
  level — identified, not yet fixed.** A column that's e.g. 85% clean
  integers and 15% genuinely non-numeric values falls below the 90%
  majority threshold (`_TYPE_MAJORITY_THRESHOLD`) and is classified STRING
  rather than INTEGER, so `TypeConsistencyCheck`'s "mixed STRING column"
  branch reports it as a single column-level note with ZERO rows flagged —
  the same probe-5 pattern as above, just below the 90% line instead of
  above it. A product review flagged this directly: the minority
  non-conforming rows in a column that's clearly "trying" to be numeric
  should flag under validity the same way they would if the column had
  cleared the 90% bar and become true INTEGER. This is a known, reproduced
  gap, not yet addressed in code.
- **Accuracy depends entirely on LLM coverage.** `semantic_rows_considered`
  can be far smaller than the table's row count (budget caps, provider
  outages, unparseable batches, quarantine). The formula is honest about its
  own denominator, but a caller that doesn't also surface
  `semantic_rows_considered` can still leave a reader assuming accuracy was
  computed over the whole table when it wasn't.
- **The severity cap allowance (20.0) is a judgment call, not a derived
  constant** — see above.
- **Upload size and semantic-reasoning cost are now bounded, but by fixed,
  judgment-call constants.** A per-upload size cap (`MAX_UPLOAD_BYTES`,
  default 20 MB) and a per-table row cap on semantic reasoning specifically
  (`MAX_SEMANTIC_REASONING_ROWS`, default 5000, opt-out via
  `allow_large_semantic_scan=true`) both exist as of `DETECTION_VERSION`
  1.10.0 — see `app/config.py` and `main.py`'s `_llm_for_table`. Neither
  number was tuned against real deployment traffic or a real cost budget;
  they're there so cost scales boundedly with file size by default, not so
  that the specific numbers are authoritative.
- **Live determinism across runs — mitigated, not guaranteed, as of
  `DETECTION_VERSION` 1.12.0.** `temperature=0` alone never guaranteed
  identical model output across separate calls to a hosted LLM API — this
  project previously only asserted that caveat without checking what either
  provider's own documentation actually says. It does, and neither promises
  determinism: Groq's API reference states, verbatim, *"If specified, our
  system will make a best effort to sample deterministically, such that
  repeated requests with the same `seed` and parameters should return the
  same result... Determinism is not guaranteed, and you should refer to the
  `system_fingerprint` response parameter to monitor changes in the
  backend."* Gemini's `seed` field is documented even more thinly ("set
  deterministic generation seed," no explicit guarantee), and Google's own
  community forum has reports of non-identical output for an identical
  prompt with a fixed seed and temperature=0 on at least one Gemini model.
  Given that, `app/agent/llm_clients.py` now pins a content-derived `seed`
  (`_deterministic_seed`) on every Groq and Gemini call, alongside
  `temperature=0` — a genuine, zero-cost improvement over leaving `seed`
  unset, but explicitly a best-effort mitigation, not a fix, since neither
  provider will commit to more than that. `scripts/check_live_determinism.py`
  is the empirical tool this limitation always needed: it sends the same
  prompt N times to each real provider and reports whether the replies were
  actually byte-identical, so a claim about reproducibility is something a
  developer measured against their own account and models, not something
  this document asserts on their behalf. Row *sampling* (which rows get
  shown to the model at all) is unaffected by any of this and remains fully
  deterministic — see `semantic_reasoning.py`'s `_content_seed`, which seeds
  this project's own `random.sample`, not the provider call.
- **PII exposure to free-tier LLM APIs — mitigated, not eliminated, as of
  `DETECTION_VERSION` 1.11.0, with a masking-design correction in 1.13.0.**
  AI review is now a true per-scan opt-in, default OFF
  (`ai_review_consent`) — nothing is sent to Groq or Gemini unless a
  request explicitly asks for it, even when the server has API keys
  configured. When it does run, email- and phone-hinted columns (plus any
  caller-named `exclude_columns`) are masked before reaching a prompt
  (`app/agent/pii_masking.py`). 1.11.0's placeholder was keyed by ROW
  INDEX — a real defect a product review reproduced: the same email in
  two different rows produced two different, uncorrelatable placeholders,
  destroying the semantic layer's ability to notice "these two records
  share a contact," one of the strongest duplicate/fraud signals in this
  kind of data. As of 1.13.0, every placeholder is keyed by the real
  VALUE (a salted hash, salt fresh per scan request) — the same value
  anywhere in a scan always produces the same token, a different value
  never collides with it, and a token carries no meaning across two
  different scans. 1.13.0 also added an EMBEDDED regex scrub covering
  every string column not already structurally masked, closing the gap
  where a column named `mobile`/`tel`/`contact` (missed by the name-based
  hint heuristic) or free text with an email/phone typed into it went out
  in the clear. Not eliminated: a column with no recognized hint, no
  email/phone-shaped substring, and not explicitly excluded still goes out
  as-is (an `address` or `job_title` column, say), names are unmasked by
  default (a deliberate scope choice, not an oversight — some semantic
  checks need real names), and the providers' own free-tier terms differ
  materially (Gemini's free tier states submitted content may be used to
  improve Google's products; Groq's don't distinguish tiers) — see
  `docs/DATA_HANDLING.md` for the full, sourced account and what it means
  for a real deployment. Out of scope for this version's SCORING logic
  specifically (nothing here changes a score), but directly relevant to
  anyone deciding whether to turn AI review on for real customer data.

## Reproducibility: a score belongs to a PAIR of versions

`SCORING_VERSION` describes the scoring *formula*; `DETECTION_VERSION`
describes what the checks and connector *flag* and what counts as
missing. The same file can score differently when only the detection
version changes (probe 5 is the example: `TypeConsistencyCheck` and
sentinel gating changed which cells are flagged, while the formula was
untouched). Therefore:

> A score is reproducible, and two scores are comparable, only when BOTH
> `detection_version` and `scoring_version` match. If either differs,
> treat the comparison as invalid until the file is rescanned under one
> version pair.

Every scan response carries both under `versions`; any UI that shows a
score should show both next to it.

## Keeping this in sync

This document corresponds to `SCORING_VERSION = "1.6.0"` in `app/version.py`.
Whenever that constant bumps, re-check every formula and worked example above
against the new code before trusting this document — a stale scoring spec
that still reads as current is worse than no spec at all.

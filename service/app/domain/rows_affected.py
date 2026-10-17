"""
Rows affected — how many rows have a PROBLEM, as opposed to how many
rows merely appear in the flagged list.

Why this exists: `null_check` flags a row for a blank in ANY column
(see app/checks/completeness.py), because letting a column excuse
itself from completeness by being mostly blank was a real, reproduced
scoring bug. That choice is right for the completeness SCORE, but it
makes the flagged-row COUNT misleading. In the 300-row live test, 66 of
the 116 "flagged" rows were flagged only because the optional `notes`
column was blank — a headline of "38.7% of rows flagged" on a file whose
real problem rate was 16.7%.

This module answers the display question only. It never feeds the
score (scorer.py is untouched), and every row stays in `flagged_rows`;
a row is just marked `counts_as_problem=False` when its ONLY findings
are blanks in columns nobody expects to be filled.

Which blanks count as problems:

- `required_columns` configured -> a blank counts iff that column is in
  the list. This is the precise rule, driven by the user.
- Nothing configured (the common case today) -> a blank counts iff the
  column is normally filled: at least OPTIONAL_BLANK_FILL_THRESHOLD of
  the table's rows have a value there. A column that is mostly filled
  makes its blanks exceptions (a missing email among 295 emails); a
  column that is often empty (notes, 26% blank here) reads as optional.
  The 0.90 threshold was picked by judgment, not tuned against labeled
  data. It is a display heuristic only — the response says which rule
  was used (`blank_policy`) so a reader never has to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.canonical.models import CanonicalTable

OPTIONAL_BLANK_FILL_THRESHOLD = 0.90

POLICY_REQUIRED_COLUMNS = "required_columns"
POLICY_FILL_RATE = "fill_rate_90"


@dataclass(frozen=True)
class RowsAffected:
    problem_rows: frozenset[int]
    optional_blank_only_rows: frozenset[int]
    policy: str


def _fill_rate(table: CanonicalTable, column: str) -> float:
    if not table.rows:
        return 1.0
    filled = sum(1 for row in table.rows if row.get(column) not in (None, ""))
    return filled / len(table.rows)


def compute_rows_affected(
    check_results: list,
    semantic_row_indices: set[int],
    flagged_row_indices: list[int],
    table: CanonicalTable,
    required_columns: list[str] | None = None,
) -> RowsAffected:
    """`check_results` are the generator's CheckResult objects,
    `semantic_row_indices` the rows the AI layer flagged, and
    `flagged_row_indices` every row in the flagged list."""
    required = {c for c in (required_columns or []) if c in table.column_names()}
    policy = POLICY_REQUIRED_COLUMNS if required else POLICY_FILL_RATE

    def blank_is_expected(column: str) -> bool:
        """True when a blank in `column` is a PROBLEM."""
        if required:
            return column in required
        return _fill_rate(table, column) >= OPTIONAL_BLANK_FILL_THRESHOLD

    expected_cache: dict[str, bool] = {}

    def expected(column: str) -> bool:
        if column not in expected_cache:
            expected_cache[column] = blank_is_expected(column)
        return expected_cache[column]

    # Rows with a finding that is NOT a null_check blank in an optional
    # column. Any other check, any semantic flag, any custom rule.
    solid: set[int] = set(semantic_row_indices)
    optional_blank_rows: set[int] = set()
    for result in check_results:
        if result.check_name != "null_check":
            solid.update(result.flagged_row_indices)
            continue
        for idx in result.flagged_row_indices:
            fields = result.flagged_fields.get(idx) or []
            if not fields or any(expected(f) for f in fields):
                solid.add(idx)  # no attribution -> be conservative, count it
            else:
                optional_blank_rows.add(idx)

    flagged = set(flagged_row_indices)
    problem = flagged & (solid | (flagged - optional_blank_rows))
    optional_only = flagged - problem
    return RowsAffected(
        problem_rows=frozenset(problem),
        optional_blank_only_rows=frozenset(optional_only),
        policy=policy,
    )

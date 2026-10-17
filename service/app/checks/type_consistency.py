from __future__ import annotations

from app.canonical.models import CanonicalTable, ColumnType, ScanContext
from app.canonical.type_conformity import parse_boolean, parse_integer_strict, parse_numeric
from app.checks.base import Check, CheckResult, MetricCategory, register

# Below this, a column's non-numeric-looking values are common enough
# that calling out a handful of "mixed" rows individually isn't useful —
# see the module docstring below. Only relevant for the STRING-column
# informational note; it never changes what gets flagged under
# INTEGER/FLOAT/BOOLEAN, which is governed by csv_connector.py's own
# 90% inference threshold.
_MIXED_COLUMN_NOTE_THRESHOLD = 0.10

_TYPED_PARSERS = {
    ColumnType.INTEGER: parse_integer_strict,
    ColumnType.FLOAT: parse_numeric,
    ColumnType.BOOLEAN: parse_boolean,
}


@register
class TypeConsistencyCheck(Check):
    """Flags a row whose value doesn't actually match its own column's
    inferred type — e.g. "abc" in a column csv_connector.py inferred as
    INTEGER because the other 95% of its values were clean integers.

    This is the validity half of fixing "probe 5" (a code review found
    that a column 40% "N/A" in an otherwise-integer column scored 100,
    since nothing checked a row's value against its column's type). The
    other half — "N/A" and friends meaning MISSING, not WRONG TYPE — is
    handled upstream at ingest (see csv_connector.py's null-sentinel
    normalization), not here: by the time a value reaches this check,
    it has already survived sentinel normalization, so what's left is a
    genuinely non-conforming value like "abc" or "12k", not a disguised
    blank. That split keeps a single bad cell from ever being counted
    under both COMPLETENESS and VALIDITY for the same underlying defect.

    Uses the exact same parsers (app/canonical/type_conformity.py) that
    csv_connector.py used to decide the column's type in the first
    place, so "does this value conform" never disagrees with "what type
    is this column" — the two most easily-decoupled questions in a
    naive implementation of this check.

    A STRING column that's NOT majority-numeric (so csv_connector.py
    correctly left it as STRING) gets no per-row flags at all here, even
    if some minority of its values happen to look numeric — that's
    exactly the "genuinely mixed/freeform column" case the plan this
    check came from called out: flagging dozens or hundreds of
    org-chart-shaped rows one at a time isn't useful. Instead, if a
    meaningful minority (>= _MIXED_COLUMN_NOTE_THRESHOLD) of a STRING
    column's values look numeric, one column-level note is added to
    `detail` — informational only, zero rows flagged — so the mixed
    shape is visible without manufacturing row-level noise.
    """

    name = "type_consistency_check"
    metric = MetricCategory.VALIDITY

    def _typed_columns(self, table: CanonicalTable) -> list[tuple[str, ColumnType]]:
        return [(c.name, c.type) for c in table.columns if c.type in _TYPED_PARSERS]

    def _mixed_string_columns(self, table: CanonicalTable) -> list[str]:
        return [c.name for c in table.columns if c.type == ColumnType.STRING]

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return bool(self._typed_columns(table)) or bool(self._mixed_string_columns(table))

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        typed_columns = [(name, _TYPED_PARSERS[col_type]) for name, col_type in self._typed_columns(table)]

        flagged: list[int] = []
        flagged_fields: dict[int, list[str]] = {}
        row_reasons: dict[int, str] = {}
        violation_count = 0

        for i, row in enumerate(table.rows):
            bad_cols: list[str] = []
            for col_name, parser in typed_columns:
                value = row.get(col_name)
                if value in (None, ""):
                    continue  # missing is COMPLETENESS's job, not this check's
                if parser(str(value)) is None:
                    bad_cols.append(col_name)
            if bad_cols:
                flagged.append(i)
                flagged_fields[i] = bad_cols
                violation_count += len(bad_cols)
                row_reasons[i] = (
                    f"value doesn't match the inferred column type for "
                    f"{', '.join(bad_cols)}"
                )

        notes: list[str] = []
        if violation_count:
            notes.append(f"{violation_count} value(s) across {len(flagged)} row(s) don't match their column's inferred type")

        for col_name in self._mixed_string_columns(table):
            values = [str(row.get(col_name)) for row in table.rows if row.get(col_name) not in (None, "")]
            if not values:
                continue
            numeric_like = sum(1 for v in values if parse_numeric(v) is not None)
            ratio = numeric_like / len(values)
            if _MIXED_COLUMN_NOTE_THRESHOLD <= ratio < 1.0 and numeric_like > 0:
                notes.append(
                    f"{col_name}: mixed-type column, {ratio * 100:.0f}% of values look "
                    f"numeric but the column didn't meet the 90% threshold to be typed "
                    f"as a number — reported here for visibility, no rows flagged for it"
                )

        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=flagged,
            total_rows_evaluated=len(table.rows),
            detail="; ".join(notes) if notes else "no type-consistency violations found",
            flagged_fields=flagged_fields,
            row_reasons=row_reasons,
        )

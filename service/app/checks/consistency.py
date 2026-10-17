from __future__ import annotations

import statistics

from app.canonical.models import CanonicalTable, ColumnType, ScanContext
from app.canonical.type_conformity import parse_numeric
from app.checks.base import Check, CheckResult, MetricCategory, register

# Below this many valid numeric values, quartiles are noise, not signal.
# Per the false-positive design discussion: better to skip a check
# entirely than run it on a sample too small to mean anything.
_MIN_VALUES = 5


@register
class OutlierCheck(Check):
    """Flags numeric values statistically far from the rest of their
    column, using the IQR method (outside Q1 - 1.5*IQR to Q3 + 1.5*IQR).
    Chosen over z-scores because IQR is less sensitive to the outliers
    themselves skewing the very baseline they're being measured against.

    Known limitation, same as the outlier false-positive discussion
    earlier: this can't distinguish a data-entry error from a real,
    legitimate rare event (a genuinely large one-off deal). It flags
    "statistically unusual," not "wrong" — the scorecard should present
    it as worth a look, not as a confirmed defect.
    """

    name = "outlier_check"
    metric = MetricCategory.CONSISTENCY

    def _numeric_columns(self, table: CanonicalTable) -> list[str]:
        return [c.name for c in table.columns if c.type in (ColumnType.INTEGER, ColumnType.FLOAT)]

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return len(self._numeric_columns(table)) > 0

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        flagged: set[int] = set()
        flagged_fields: dict[int, list[str]] = {}
        notes: list[str] = []

        for col in self._numeric_columns(table):
            indexed_values: list[tuple[int, float]] = []
            for i, row in enumerate(table.rows):
                v = row.get(col)
                if v in (None, ""):
                    continue
                # Shared with type inference and TypeConsistencyCheck
                # (app/canonical/type_conformity.py) — recognizes
                # currency ("$1,200"), thousands separators, and
                # percentages as numeric values instead of silently
                # skipping them the way a bare float(v) would. A value
                # that doesn't parse even with that is left out of the
                # outlier computation entirely (same as before), not
                # treated as 0 or flagged on its own — that's
                # TypeConsistencyCheck's job, not this one's.
                parsed = parse_numeric(str(v))
                if parsed is None:
                    continue
                indexed_values.append((i, parsed))

            if len(indexed_values) < _MIN_VALUES:
                continue

            nums = sorted(v for _, v in indexed_values)
            q1, _, q3 = statistics.quantiles(nums, n=4)
            iqr = q3 - q1
            if iqr == 0:
                continue  # no spread at all -> nothing meaningful to flag

            lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            col_flagged = [i for i, v in indexed_values if v < lower or v > upper]
            if col_flagged:
                notes.append(f"{col}: {len(col_flagged)} outside [{lower:.1f}, {upper:.1f}]")
                flagged.update(col_flagged)
                for i in col_flagged:
                    flagged_fields.setdefault(i, []).append(col)

        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=sorted(flagged),
            total_rows_evaluated=len(table.rows),
            detail="; ".join(notes) if notes else "no statistical outliers found",
            flagged_fields=flagged_fields,
        )

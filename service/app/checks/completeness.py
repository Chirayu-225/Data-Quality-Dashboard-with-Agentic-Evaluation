from __future__ import annotations

from app.canonical.models import CanonicalTable, ScanContext
from app.checks.base import Check, CheckResult, MetricCategory, register


@register
class NullCheck(Check):
    """Flags rows with a null/blank value in ANY column, required or
    not — required-field blanks and optional-field blanks are both
    reported, distinguished downstream by weight (see
    app/scoring/scorer.py's field-criticality weighting: required =
    2.0, optional = 1.0), never by one of them being invisible to this
    check entirely.

    This used to only look at required_cols (`not c.nullable`). That
    was a real, reproduced bug, not a style choice: for the CSV
    connector, `nullable` itself is INFERRED from this same column's
    own blank ratio (>=50% blank -> nullable=True — see
    csv_connector.py). Gating the check on that inferred flag meant a
    column was excused from completeness scoring in exact proportion
    to how broken it was — a column blank in 40% of rows was correctly
    flagged (still under the 50% inference threshold), but blank in
    60% of rows was WORSE and scored completeness=100.0, a perfect
    score, because crossing 50% blank flipped it to "optional" and
    NullCheck stopped looking at it entirely. Reproduced directly: two
    otherwise-identical probe tables (40% vs 60% blank email) scored
    84.0 and 100.0 overall — worse data scoring strictly higher. The
    check is grading itself with the answer key it wrote from the
    exam data.

    Flagging every blank column (at the appropriate weight) removes
    that circularity without needing a real source-of-truth for
    requiredness (SQL NOT NULL, a Salesforce field definition, or user
    configuration) — none of which exist yet without the persistence/
    config layer this project has deliberately frozen for this phase.
    This is an accepted interim trade-off: a genuinely optional field
    that's usually blank now shows up in the completeness metric too,
    just at the lower OPTIONAL weight rather than being invisible —
    better than letting the worst-case column disappear from scoring
    altogether."""

    name = "null_check"
    metric = MetricCategory.COMPLETENESS

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return len(table.columns) > 0

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        all_cols = [c.name for c in table.columns]
        flagged: list[int] = []
        flagged_fields: dict[int, list[str]] = {}
        for i, row in enumerate(table.rows):
            blank = [col for col in all_cols if row.get(col) in (None, "")]
            if blank:
                flagged.append(i)
                flagged_fields[i] = blank
        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=flagged,
            total_rows_evaluated=len(table.rows),
            detail=f"{len(flagged)} rows have a blank field",
            flagged_fields=flagged_fields,
            row_reasons={
                i: "blank in " + ", ".join(cols) for i, cols in flagged_fields.items()
            },
        )

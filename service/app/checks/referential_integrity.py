from __future__ import annotations

from app.canonical.models import CanonicalTable, ScanContext
from app.checks.base import Check, CheckResult, MetricCategory, register

# Naming heuristic for detecting candidate foreign keys, same spirit as
# the CSV connector's semantic-hint inference: a column named
# "account_id" is presumed to reference a table named "accounts",
# column "id". This is a guess, not real schema metadata — it'll be
# replaced by actual foreign-key metadata once the SQL and Salesforce
# connectors exist, same as the format-validity hint inference.
_FK_SUFFIX = "_id"
_PK_COLUMN = "id"


def _related_table_name(fk_column: str) -> str:
    base = fk_column[: -len(_FK_SUFFIX)]
    return base if base.endswith("s") else base + "s"


@register
class ReferentialIntegrityCheck(Check):
    """Flags foreign-key-shaped values that don't point at a real row.

    Two modes, chosen automatically based on what's available:
    - If the related table is present in the scan's ScanContext: a
      REAL check — does this account_id actually exist in the accounts
      table's id column.
    - If no related table is available (the common case for a single
      CSV upload with no second file): there's nothing to verify
      non-blank values against, and a blank FK value is ALREADY a
      Finding under COMPLETENESS (NullCheck flags a blank in every
      column, this one included — see completeness.py). Flagging it
      again here, under CONSISTENCY, used to double-count the exact
      same defect under two separate metrics for no added signal — a
      code review reproduced this directly (a table with a blank FK
      column showed the identical row set flagged under both metrics).
      Fallback mode therefore reports the fact in `detail` (so a caller
      still sees that this FK's non-blank values went unverified) but
      contributes NO flagged rows of its own; the blank cell itself is
      scored once, under COMPLETENESS, where it already belongs.
    """

    name = "referential_integrity_check"
    metric = MetricCategory.CONSISTENCY

    def _fk_columns(self, table: CanonicalTable) -> list[str]:
        return [
            name
            for name in table.column_names()
            if name.endswith(_FK_SUFFIX) and name != _PK_COLUMN
        ]

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return len(self._fk_columns(table)) > 0

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        flagged: set[int] = set()
        flagged_fields: dict[int, list[str]] = {}
        notes: list[str] = []

        for fk_col in self._fk_columns(table):
            related_name = _related_table_name(fk_col)
            related_table = context.get_table(related_name) if context else None

            if related_table is not None:
                valid_ids = {
                    str(row.get(_PK_COLUMN))
                    for row in related_table.rows
                    if row.get(_PK_COLUMN) not in (None, "")
                }
                bad = [
                    i
                    for i, row in enumerate(table.rows)
                    if row.get(fk_col) not in (None, "")
                    and str(row.get(fk_col)) not in valid_ids
                ]
                if bad:
                    notes.append(
                        f"{fk_col}: {len(bad)} rows reference missing "
                        f"{related_name} records"
                    )
                    flagged.update(bad)
                    for i in bad:
                        flagged_fields.setdefault(i, []).append(fk_col)
            else:
                # Fallback mode — no related table to verify against.
                # Blank values are NOT flagged here: NullCheck already
                # flags them under COMPLETENESS, and flagging them again
                # under CONSISTENCY would double-count the same defect
                # (see this check's own docstring). Non-blank values are
                # simply unverifiable without the related table — noted,
                # not flagged, since there is genuinely no evidence
                # either way.
                blank_count = sum(1 for row in table.rows if row.get(fk_col) in (None, ""))
                if blank_count:
                    notes.append(
                        f"{fk_col}: {blank_count} row(s) have a blank reference "
                        f"(no '{related_name}' table available to verify "
                        f"existence against — fallback mode; already counted "
                        f"under completeness, not flagged again here)"
                    )

        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=sorted(flagged),
            total_rows_evaluated=len(table.rows),
            detail="; ".join(notes) if notes else "no referential integrity issues found",
            flagged_fields=flagged_fields,
        )

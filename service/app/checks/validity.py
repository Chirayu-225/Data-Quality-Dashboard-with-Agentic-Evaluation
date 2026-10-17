from __future__ import annotations

import re
from datetime import datetime

from app.canonical.models import CanonicalTable, ScanContext
from app.checks.base import Check, CheckResult, MetricCategory, register

# Deliberately permissive — per the false-positive design discussion,
# it's better to under-flag on format than to annoy users with garbage
# flags on valid-but-unusual data (plus-addressing, new TLDs, etc.).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# May start with "+" (country code) or "(" (US area code in parentheses,
# "(555) 123-4567" — one of the most common formats there is; the original
# pattern required a leading "+" or digit and flagged every one of them).
_PHONE_PATTERN = re.compile(r"^(\+|\()?\d[\d\s\-().]{5,18}\d$")
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"]


def _is_valid_date(value: str) -> bool:
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


_VALIDATORS = {
    "email": lambda v: bool(_EMAIL_PATTERN.match(v)),
    "phone": lambda v: bool(_PHONE_PATTERN.match(v)),
    "date": _is_valid_date,
}


@register
class FormatValidityCheck(Check):
    """Validates columns with a recognized semantic hint (email, phone,
    date) against a permissive pattern. Blank values are intentionally
    skipped here — that's NullCheck's job, not this check's, so a
    single missing value doesn't get flagged twice under two different
    metrics."""

    name = "format_validity_check"
    metric = MetricCategory.VALIDITY

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return any(c.semantic_hint in _VALIDATORS for c in table.columns)

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        hinted_columns = [
            (c.name, c.semantic_hint)
            for c in table.columns
            if c.semantic_hint in _VALIDATORS
        ]

        flagged: list[int] = []
        flagged_fields: dict[int, list[str]] = {}
        bad_field_count = 0
        for i, row in enumerate(table.rows):
            bad_cols: list[str] = []
            for col_name, hint in hinted_columns:
                value = row.get(col_name)
                if value in (None, ""):
                    continue  # NullCheck's territory, not this check's
                if not _VALIDATORS[hint](str(value)):
                    bad_cols.append(col_name)
                    bad_field_count += 1
            if bad_cols:
                flagged.append(i)
                flagged_fields[i] = bad_cols

        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=flagged,
            total_rows_evaluated=len(table.rows),
            detail=f"{bad_field_count} field values failed format validation "
            f"across {len(flagged)} rows",
            flagged_fields=flagged_fields,
        )

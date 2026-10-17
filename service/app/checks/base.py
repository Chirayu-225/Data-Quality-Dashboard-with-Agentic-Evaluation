"""
Deterministic check registry.

Each check is independent and composable: it takes a CanonicalTable and
returns flagged rows + a metric contribution. New checks register
themselves without touching existing ones — this is what the agent's
"tool execution" step actually calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel

from app.canonical.models import CanonicalTable, ScanContext


class MetricCategory(str, Enum):
    """The 5 scorecard metrics every check must declare itself under."""

    COMPLETENESS = "completeness"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"
    CONSISTENCY = "consistency"
    ACCURACY = "accuracy"


class CheckResult(BaseModel):
    check_name: str
    metric: MetricCategory
    flagged_row_indices: list[int]
    total_rows_evaluated: int
    detail: str
    # Optional field-level attribution: row_index -> which specific
    # column(s) caused that row to be flagged. Backward compatible —
    # defaults to empty, and checks where "which field" doesn't really
    # apply (DuplicateCheck: duplication is a whole-row property, not
    # one field's fault) simply leave it empty. This is what lets the
    # scorer weight a flag on a required field more heavily than one
    # on an optional field, instead of treating every flagged row
    # identically regardless of which column caused it.
    flagged_fields: dict[int, list[str]] = {}
    # Optional per-row confidence tier: row_index -> "high" | "low".
    # Same backward-compatible pattern as flagged_fields above — empty
    # for every check except PromptInjectionCheck, which uses this to
    # distinguish an unambiguous manipulation attempt (quarantine it —
    # see app/agent/generator.py) from ambiguous business-sounding
    # language that merely LOOKS similar (report it, but don't remove
    # the row from semantic review over it). A row absent from this
    # dict is treated as "high" by convention (the conservative
    # default for any check that doesn't populate it at all).
    row_severity: dict[int, str] = {}
    # Optional per-row reason: row_index -> a specific, human-readable
    # explanation for why THIS row was flagged — as opposed to `detail`
    # above, which is one aggregate string for the WHOLE check (e.g.
    # "email: 1 row(s) share a business key...; name+company: 3 row(s)
    # share a business key..."), reused identically for every flagged
    # row regardless of which specific match caused that particular
    # row's flag. A code review surfaced this as confusing on
    # DuplicateCheck, whose aggregate detail could mention a
    # name+company match on a row that was actually only caught by the
    # email signal. Backward compatible — defaults to empty; a
    # consumer falls back to the aggregate `detail` string for any row
    # not present here, which is every row for a check that doesn't
    # populate this at all.
    row_reasons: dict[int, str] = {}


class Check(ABC):
    """Base class for a deterministic check."""

    name: str
    metric: MetricCategory

    @abstractmethod
    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        """Whether this check is relevant given the table's schema
        (e.g. a format-validity check for emails only applies to
        columns hinted as `email`). `context` is only needed by checks
        that look across tables (referential integrity); most checks
        ignore it."""
        raise NotImplementedError

    @abstractmethod
    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        raise NotImplementedError


_REGISTRY: list[type[Check]] = []


def register(check_cls: type[Check]) -> type[Check]:
    """Decorator: adding a new check is just `@register` on the class —
    nothing else in the system needs to change."""
    _REGISTRY.append(check_cls)
    return check_cls


def all_checks() -> list[type[Check]]:
    return list(_REGISTRY)

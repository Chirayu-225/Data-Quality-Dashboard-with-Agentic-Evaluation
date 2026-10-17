"""
Finding — the canonical, unified representation of "something the
evaluator discovered," as distinct from CheckResult ("what the
evaluator did"). Before this module, the codebase had three separate
species of "something went wrong" — CheckResult (deterministic),
SemanticFlag (single-table semantic), and CrossTableSemanticFinding
(cross-table semantic) — each with a different shape, which meant the
scorer had to understand each species individually.

This is deliberately an ADDITIVE refactor, not a replacement:
CheckResult, ScanResult, SemanticFlag, VerifiedFlag, and
CrossTableSemanticFinding all keep their existing fields untouched, so
the public API response shape and the frontend are unaffected. Finding
is built via converters from those existing types, consumed
internally by the scorer, and available for future evidence/UI work.

Design decisions locked in before implementation (see conversation):
- `category` reuses MetricCategory — no new taxonomy.
- `check_type` is a separate axis from `category`: which layer
  produced this, not which quality dimension it affects.
- `severity` is DERIVED, not independently assigned by every check —
  from field criticality (nullable) for deterministic findings, and
  from confidence + verification_status for semantic findings. It is
  informational/display-only in this pass; the scorer computes its
  own numeric weight from the same underlying source facts, not by
  reversing severity back into a number, so scoring behavior is
  provably unchanged (see app/scoring/scorer.py).
- `verification_status` is a first-class field (not buried in
  metadata), since scorer.py already branches on it directly.
- Finding identity is a DETERMINISTIC hash, not a random UUID — so a
  before/after regression diff (same dataset, same code path, run
  twice) produces identical Finding IDs, which is what makes "did the
  refactor change behavior" provable rather than asserted.
- REJECTED semantic/cross-table flags never become a Finding at all —
  same exclusion the pre-refactor code already applied before
  building flagged_row_indices; the converter just does it up front.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Optional, Sequence

from pydantic import BaseModel

from app.checks.base import CheckResult, MetricCategory
from app.domain.confidence import derive_confidence


class CheckType(str, Enum):
    DETERMINISTIC = "deterministic"
    SEMANTIC_SINGLE_TABLE = "semantic_single_table"
    SEMANTIC_CROSS_TABLE = "semantic_cross_table"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class VerificationStatus(str, Enum):
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    # Deliberately no UNVERIFIED member — "no verification ran" is
    # represented as `verification_status=None` on the Finding itself,
    # not a fourth enum value, so a caller can't confuse "verified as
    # uncertain" with "never checked at all."


def generate_finding_id(
    check_type: str,
    category: str,
    affected_dataset: str,
    affected_rows: Optional[Sequence[int]] = None,
    affected_fields: Optional[Sequence[str]] = None,
) -> str:
    """Deterministic content-hash identity — same inputs always
    produce the same id, in the same run or a re-run of the same
    dataset through the same code path. This is what makes a
    before/after Finding-set diff meaningful: it's stable across
    reruns of the SAME content, not a random ID space, and it isn't
    meant to be globally unique across unrelated scans of unrelated
    data — that's an intentional scope limit given there's no
    persistence layer to disambiguate scans yet.

    Rows and fields are sorted before joining so that iteration-order
    differences (e.g. a set vs a list upstream) never change the hash
    for what is semantically the same finding. A cross-table finding's
    two-sided identity (child row + parent row, or child table +
    parent table) is expected to already be folded into
    `affected_dataset` (e.g. "accounts->contacts") and `affected_rows`
    (both row indices) by the caller, rather than needing a separate
    signature — see findings_from_cross_table_semantic below.
    """
    safe_fields = "|".join(sorted(affected_fields)) if affected_fields else "SCOPE_ALL_COLS"
    safe_rows = "|".join(sorted(str(r) for r in affected_rows)) if affected_rows else "SCOPE_DATASET"
    seed = f"{affected_dataset}|{check_type}|{category}|{safe_rows}|{safe_fields}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class Finding(BaseModel):
    id: str
    category: MetricCategory
    check_type: CheckType
    affected_dataset: str
    affected_rows: list[int] = []
    affected_fields: list[str] = []
    severity: Severity
    confidence: float
    # A second, independently-derived confidence figure — see
    # app/domain/confidence.py. `confidence` above stays the raw,
    # self-reported number from whichever layer produced this Finding
    # (1.0 for deterministic checks, the generator's own claim for
    # semantic ones); `derived_confidence` is this module's own
    # estimate, built from structural facts rather than trusting that
    # raw number. Deliberately NOT wired into `severity` this pass —
    # severity keeps deriving from the raw confidence, unchanged, so
    # this stays a purely additive signal rather than touching anything
    # already proven behavior-preserving (see scorer.py).
    derived_confidence: float
    evidence: dict[str, Any] | None = None
    explanation: str
    verification_status: VerificationStatus | None = None
    source: str
    metadata: dict[str, Any] = {}


# --- Severity derivation -----------------------------------------------
# Deliberately separate from scoring weight (see scorer.py) — severity
# is a human-facing categorical label; the scorer derives its own
# numeric weight from the same underlying facts in parallel, so
# neither has to be reverse-engineered from the other.

def _deterministic_severity(fields: list[str], nullable_by_field: dict[str, bool]) -> Severity:
    if not fields:
        return Severity.MEDIUM
    # A required (non-nullable) field involved makes this the more
    # severe case — mirrors scorer.py's existing field-criticality
    # weighting (required -> weighs double), just expressed as a label.
    if any(not nullable_by_field.get(f, True) for f in fields):
        return Severity.HIGH
    return Severity.MEDIUM


def _semantic_severity(confidence: float, verification_status: VerificationStatus | None) -> Severity:
    if verification_status == VerificationStatus.CONFIRMED and confidence >= 0.8:
        return Severity.HIGH
    if verification_status == VerificationStatus.CONFIRMED:
        return Severity.MEDIUM
    if verification_status == VerificationStatus.NEEDS_REVIEW or verification_status is None:
        return Severity.MEDIUM if confidence >= 0.5 else Severity.LOW
    return Severity.LOW


# --- Converters ----------------------------------------------------------

def findings_from_check_result(
    check_result: CheckResult,
    table_name: str,
    nullable_by_field: dict[str, bool],
) -> list[Finding]:
    """One Finding per flagged row (not one per check overall) —
    matches the row-level granularity CheckResult.flagged_row_indices
    already implies. `explanation` prefers the check's row-specific
    reason (CheckResult.row_reasons) when populated, falling back to
    the check's aggregate `detail` string otherwise — a check that
    doesn't populate row_reasons (most of them, today) behaves exactly
    as before."""
    findings: list[Finding] = []
    for row_idx in check_result.flagged_row_indices:
        fields = check_result.flagged_fields.get(row_idx, [])
        findings.append(
            Finding(
                id=generate_finding_id(
                    CheckType.DETERMINISTIC.value,
                    check_result.metric.value,
                    table_name,
                    [row_idx],
                    fields,
                ),
                category=check_result.metric,
                check_type=CheckType.DETERMINISTIC,
                affected_dataset=table_name,
                affected_rows=[row_idx],
                affected_fields=fields,
                severity=_deterministic_severity(fields, nullable_by_field),
                confidence=1.0,
                derived_confidence=derive_confidence(CheckType.DETERMINISTIC.value, None),
                explanation=check_result.row_reasons.get(row_idx, check_result.detail),
                verification_status=None,
                source=check_result.check_name,
                metadata={},
            )
        )
    return findings


def findings_from_semantic_flags(
    flags: list,
    verified_by_row: dict[int, object],
    table_name: str,
) -> list[Finding]:
    """`flags` is list[SemanticFlag], `verified_by_row` maps
    row_index -> VerifiedFlag. Untyped in the signature to avoid a
    circular import (app.agent.generator imports app.scoring.scorer,
    which will import this module) — duck-typed on the attributes
    every caller's real objects already have (.row_index, .reason,
    .confidence, .label, .verifier_notes).

    A REJECTED verification excludes the row from becoming a Finding
    at all — same exclusion GeneratorAgent.ScanResult already applies
    before building flagged_row_indices; this just applies it up
    front rather than relying on the caller to have filtered first."""
    findings: list[Finding] = []
    for flag in flags:
        verified = verified_by_row.get(flag.row_index)
        verification_status = None
        verifier_notes = None
        verification_failed = False
        if verified is not None:
            if verified.label.value == "rejected":
                continue
            verification_status = VerificationStatus(verified.label.value)
            verifier_notes = verified.verifier_notes
            # getattr-defaulted: duck-typed callers/test doubles that
            # predate this field simply read as "not a technical
            # failure," never crash.
            verification_failed = getattr(verified, "verification_failed", False)

        findings.append(
            Finding(
                id=generate_finding_id(
                    CheckType.SEMANTIC_SINGLE_TABLE.value,
                    MetricCategory.ACCURACY.value,
                    table_name,
                    [flag.row_index],
                    None,
                ),
                category=MetricCategory.ACCURACY,
                check_type=CheckType.SEMANTIC_SINGLE_TABLE,
                affected_dataset=table_name,
                affected_rows=[flag.row_index],
                affected_fields=[],
                severity=_semantic_severity(flag.confidence, verification_status),
                confidence=flag.confidence,
                derived_confidence=derive_confidence(
                    CheckType.SEMANTIC_SINGLE_TABLE.value,
                    verification_status.value if verification_status else None,
                    verification_failed,
                ),
                explanation=flag.reason,
                verification_status=verification_status,
                source="semantic_reasoner",
                metadata={"verifier_notes": verifier_notes} if verifier_notes else {},
            )
        )
    return findings


def findings_from_cross_table_semantic(cross_table_findings: list) -> list[Finding]:
    """`cross_table_findings` is list[CrossTableSemanticFinding] (the
    main.py response model — already REJECTED-filtered at that call
    site today). Kept as a standalone converter for future evidence/UI
    use; NOT currently wired into any scoring path, since cross-table
    semantic findings aren't scored today either (SchemaScanResponse's
    aggregate_score is a row-count-weighted average of each table's
    own single-table scorecard) — this converter doesn't change that,
    only gives those findings the same representation as the others."""
    findings: list[Finding] = []
    for f in cross_table_findings:
        dataset = f"{f.from_table}->{f.to_table}"
        rows = [f.from_row_index, f.to_row_index]
        verification_status = (
            VerificationStatus(f.verification_label) if f.verification_label else None
        )
        # getattr-defaulted for the same reason as findings_from_semantic_flags above.
        verification_failed = getattr(f, "verification_failed", False)
        findings.append(
            Finding(
                id=generate_finding_id(
                    CheckType.SEMANTIC_CROSS_TABLE.value,
                    MetricCategory.ACCURACY.value,
                    dataset,
                    rows,
                    [f.fk_column],
                ),
                category=MetricCategory.ACCURACY,
                check_type=CheckType.SEMANTIC_CROSS_TABLE,
                affected_dataset=dataset,
                affected_rows=rows,
                affected_fields=[f.fk_column],
                severity=_semantic_severity(f.confidence, verification_status),
                confidence=f.confidence,
                derived_confidence=derive_confidence(
                    CheckType.SEMANTIC_CROSS_TABLE.value,
                    verification_status.value if verification_status else None,
                    verification_failed,
                ),
                explanation=f.reason,
                verification_status=verification_status,
                source="cross_table_reasoner",
                metadata={"verifier_notes": f.verification_notes} if f.verification_notes else {},
            )
        )
    return findings

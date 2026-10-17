"""
Two-version comparison.

Given an older and a newer version of the same table, report what
changed, using only deterministic and statistical measurements, no LLM:

  * schema   - columns added/removed, probable renames, type changes,
               column-order change
  * rows     - matched by a KEY column (never by position): unchanged /
               modified / added / removed, and which columns changed
  * volume   - row-count change
  * nulls    - per-column null-rate change
  * drift    - PSI per column (categorical and numeric), banded by
               configurable thresholds
  * hashes   - an order-independent, format-normalized content hash of
               each version over the shared columns

Design notes:

  * Matching is by key. If no usable key exists, the comparison falls
    back to counting identical normalized rows (added/removed only; it
    cannot say "modified") and says so in `warnings`.
  * Values are NORMALIZED before comparison: surrounding whitespace is
    stripped and anything that parses as a number is compared by its
    exact decimal value, so "$12,345.00" equals "12345". A reformat
    therefore shows up as a schema/type change, not as 1,000 modified
    rows. Strings otherwise compare exactly.
  * A hash mismatch proves content changed. It says nothing about who
    changed it or why, and this module never claims otherwise.
  * Drift thresholds are heuristics. They are parameters, not truth.
  * Nothing here is persisted.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from app.canonical.models import CanonicalTable, ColumnType
from app.canonical.type_conformity import _CURRENCY_CHARS
from app.version import COMPARISON_VERSION
from app.comparison.profile import (
    TableProfile,
    finite_number,
    profile_table,
)

PSI_EPSILON = 1e-4
_PSI_BINS = 10
_KEY_NAME_RE = re.compile(r"(^|[_\s-])id$|^(uuid|guid|key)$", re.IGNORECASE)
_RENAME_AGREEMENT_MIN = 0.95
_RENAME_MIN_SUPPORT = 20
_NUMERIC_MIN_VALUES = 30
_NUMERIC_MIN_PARSE_RATE = 0.9
_CATEGORICAL_MAX_DISTINCT = 50
_CATEGORICAL_MAX_DISTINCT_RATIO = 0.1


class ComparisonError(ValueError):
    """A caller-fixable problem (bad key column, etc.), not a bug."""


# ---------------------------------------------------------------- models


class RenameProposal(BaseModel):
    old_name: str
    new_name: str
    agreement: float
    basis: str


class TypeChange(BaseModel):
    column: str
    old_type: ColumnType
    new_type: ColumnType
    # "format" = an integer<->float change on a column whose values are
    # unchanged for every matched row (e.g. 12345 -> 12,345.00): the file
    # is presented differently, the data is not. Only decidable when rows
    # were matched by key; otherwise "type". Never guessed.
    kind: str = "type"
    values_unchanged: bool | None = None


class SchemaDiff(BaseModel):
    added_columns: list[str]
    removed_columns: list[str]
    renamed: list[RenameProposal]
    type_changes: list[TypeChange]
    column_order_changed: bool
    shared_columns: list[str]


class FieldChange(BaseModel):
    column: str
    old: str
    new: str


class ModifiedRow(BaseModel):
    key: str
    changed_fields: list[FieldChange]


class RowDiff(BaseModel):
    matching: str  # "by_key" | "by_row_hash"
    unchanged: int
    modified: int | None  # None when matching by row hash (cannot tell)
    added: int
    removed: int
    modified_by_column: dict[str, int]
    modified_rows: list[ModifiedRow]
    added_keys: list[str]
    removed_keys: list[str]
    detail_truncated: bool


class NullRateChange(BaseModel):
    column: str
    old_rate: float
    new_rate: float
    delta: float
    flagged: bool


class DriftResult(BaseModel):
    column: str
    kind: str | None  # "numeric" | "categorical" | None when not evaluated
    metric: str = "psi"
    value: float | None
    band: str | None  # "stable" | "warning" | "critical" | None
    not_evaluated_reason: str | None = None


class HashSummary(BaseModel):
    method: str
    old_content_hash: str
    new_content_hash: str
    identical: bool


class ComparisonFinding(BaseModel):
    type: str
    detector_type: str  # "deterministic" | "statistical" | "integrity"
    column: str | None
    metric: str
    observed: float | str | None
    baseline: float | str | None
    threshold: float | str | None
    deviation_band: str | None
    message: str


class ComparisonResult(BaseModel):
    comparison_version: str = COMPARISON_VERSION
    old_name: str
    new_name: str
    old_rows: int
    new_rows: int
    volume_change_pct: float | None
    volume_flagged: bool
    key_column: str | None
    schema_diff: SchemaDiff
    rows: RowDiff
    null_rate_changes: list[NullRateChange]
    drift: list[DriftResult]
    hashes: HashSummary
    findings: list[ComparisonFinding]
    summary: str
    warnings: list[str]
    thresholds: dict[str, float]
    old_profile: TableProfile
    new_profile: TableProfile


# ------------------------------------------------------------ normalization


def normalize_cell(value: object) -> str:
    """Canonical text for comparison: stripped; numbers by exact value."""
    s = ("" if value is None else str(value)).strip()
    if s == "":
        return ""
    if finite_number(s) is not None:
        t = s
        if t[0] in _CURRENCY_CHARS:
            t = t[1:]
        t = t.replace(",", "")
        if t.endswith("%"):
            t = t[:-1]
        try:
            d = Decimal(t)
        except InvalidOperation:
            return "s:" + s
        if d == 0:
            return "n:0"
        return "n:" + format(d.normalize(), "f")
    return "s:" + s


def _row_hash(cells: list[str]) -> str:
    return hashlib.sha256("\x1f".join(cells).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- PSI


def psi_band(value: float, warn: float, critical: float) -> str:
    if value >= critical:
        return "critical"
    if value >= warn:
        return "warning"
    return "stable"


def _psi(new_p: list[float], old_p: list[float]) -> float:
    return sum((n - o) * math.log(n / o) for n, o in zip(new_p, old_p))


def categorical_psi(old_values: list[str], new_values: list[str]) -> float:
    cats = sorted(set(old_values) | set(new_values))
    old_c, new_c = Counter(old_values), Counter(new_values)
    old_p = [max(old_c[c] / len(old_values), PSI_EPSILON) for c in cats]
    new_p = [max(new_c[c] / len(new_values), PSI_EPSILON) for c in cats]
    return _psi(new_p, old_p)


def numeric_psi(old_values: list[float], new_values: list[float], bins: int = _PSI_BINS) -> float:
    """Quantile bins are cut from the OLD (baseline) values. Duplicate
    edges are dropped so heavy ties cannot create empty bins."""
    old_sorted = sorted(old_values)
    edges = sorted({old_sorted[int(len(old_sorted) * k / bins)] for k in range(1, bins)})
    n_bins = len(edges) + 1

    def hist(values: list[float]) -> list[float]:
        counts = [0] * n_bins
        for v in values:
            counts[bisect.bisect_right(edges, v)] += 1
        return [max(c / len(values), PSI_EPSILON) for c in counts]

    return _psi(hist(new_values), hist(old_values))


# --------------------------------------------------------------- helpers


def _values(table: CanonicalTable, column: str) -> list[str]:
    return [(row.get(column) or "").strip() for row in table.rows]


def _is_unique_nonblank(values: list[str]) -> bool:
    return all(v != "" for v in values) and len(set(values)) == len(values)


def _detect_key(
    old: CanonicalTable, new: CanonicalTable, shared: list[str], requested: str | None
) -> tuple[str | None, str | None]:
    """Returns (key, warning). A requested key must be valid or it is an
    error; an auto-detected key is silently skipped if not usable."""
    if requested:
        if requested not in shared:
            raise ComparisonError(
                f"key column '{requested}' must exist in both files (shared columns: {shared})"
            )
        for label, t in (("older", old), ("newer", new)):
            if not _is_unique_nonblank(_values(t, requested)):
                raise ComparisonError(
                    f"key column '{requested}' is not unique and non-blank in the {label} file"
                )
        return requested, None
    candidates = [c for c in shared if _KEY_NAME_RE.search(c)]
    candidates.sort(key=lambda c: (c.lower() != "id", c))
    for c in candidates:
        if _is_unique_nonblank(_values(old, c)) and _is_unique_nonblank(_values(new, c)):
            return c, None
    return None, (
        "No usable key column was found (an 'id'-style column that is unique and non-blank in "
        "both files), so rows were compared by content: added and removed rows can be counted, "
        "but modified rows cannot be identified. Pass key_column to match rows by key."
    )


def _column_types(table: CanonicalTable) -> dict[str, ColumnType]:
    return {c.name: c.type for c in table.columns}


def _detect_renames(
    old: CanonicalTable,
    new: CanonicalTable,
    removed: list[str],
    added: list[str],
    key: str | None,
) -> list[RenameProposal]:
    if not removed or not added:
        return []
    candidates: list[tuple[float, str, str, str]] = []
    if key:
        old_by_key = {(r.get(key) or "").strip(): r for r in old.rows}
        pairs = [(old_by_key[(r.get(key) or "").strip()], r) for r in new.rows
                 if (r.get(key) or "").strip() in old_by_key]
        for r_col in removed:
            for a_col in added:
                agree = total = 0
                for o_row, n_row in pairs:
                    ov, nv = normalize_cell(o_row.get(r_col)), normalize_cell(n_row.get(a_col))
                    if ov == "" and nv == "":
                        continue
                    total += 1
                    agree += ov == nv
                if total >= _RENAME_MIN_SUPPORT:
                    candidates.append(
                        (agree / total, r_col, a_col, f"values agree on {agree} of {total} matched rows")
                    )
    else:
        for r_col in removed:
            ov = {normalize_cell(v) for v in _values(old, r_col)} - {""}
            for a_col in added:
                nv = {normalize_cell(v) for v in _values(new, a_col)} - {""}
                union = ov | nv
                if len(union) >= _RENAME_MIN_SUPPORT:
                    j = len(ov & nv) / len(union)
                    candidates.append((j, r_col, a_col, f"distinct values overlap {j:.0%}"))
    candidates.sort(reverse=True)
    used_old: set[str] = set()
    used_new: set[str] = set()
    out: list[RenameProposal] = []
    for score, r_col, a_col, basis in candidates:
        if score < _RENAME_AGREEMENT_MIN or r_col in used_old or a_col in used_new:
            continue
        used_old.add(r_col)
        used_new.add(a_col)
        out.append(RenameProposal(old_name=r_col, new_name=a_col, agreement=round(score, 4), basis=basis))
    return out


def _drift_for_column(
    column_old: str,
    column_new: str,
    old: CanonicalTable,
    new: CanonicalTable,
    key: str | None,
    warn: float,
    critical: float,
) -> DriftResult:
    label = column_new

    def skip(reason: str) -> DriftResult:
        return DriftResult(column=label, kind=None, value=None, band=None, not_evaluated_reason=reason)

    if key and column_new == key or _KEY_NAME_RE.search(column_new):
        return skip("identifier column; a distribution comparison is not meaningful")
    old_raw = [v for v in _values(old, column_old) if v != ""]
    new_raw = [v for v in _values(new, column_new) if v != ""]
    if len(old_raw) < _NUMERIC_MIN_VALUES or len(new_raw) < _NUMERIC_MIN_VALUES:
        return skip(f"fewer than {_NUMERIC_MIN_VALUES} non-blank values in one version")

    old_num = [x for x in (finite_number(v) for v in old_raw) if x is not None]
    new_num = [x for x in (finite_number(v) for v in new_raw) if x is not None]
    if (
        len(old_num) / len(old_raw) >= _NUMERIC_MIN_PARSE_RATE
        and len(new_num) / len(new_raw) >= _NUMERIC_MIN_PARSE_RATE
    ):
        value = numeric_psi(old_num, new_num)
        return DriftResult(column=label, kind="numeric", value=round(value, 4), band=psi_band(value, warn, critical))

    old_distinct = len(set(old_raw))
    if (
        old_distinct <= _CATEGORICAL_MAX_DISTINCT
        and old_distinct / len(old_raw) <= _CATEGORICAL_MAX_DISTINCT_RATIO
    ):
        value = categorical_psi(old_raw, new_raw)
        return DriftResult(column=label, kind="categorical", value=round(value, 4), band=psi_band(value, warn, critical))
    return skip("high-cardinality or free-text column; PSI is not meaningful")


# ------------------------------------------------------------------ main


def compare_tables(
    old: CanonicalTable,
    new: CanonicalTable,
    *,
    key_column: str | None = None,
    psi_warn: float = 0.10,
    psi_critical: float = 0.20,
    null_rate_change_warn: float = 0.05,
    volume_change_warn_pct: float = 10.0,
    max_detail_rows: int = 200,
) -> ComparisonResult:
    if psi_warn >= psi_critical:
        raise ComparisonError("psi_warn must be lower than psi_critical")
    warnings: list[str] = []

    old_cols = [c.name for c in old.columns]
    new_cols = [c.name for c in new.columns]
    shared_exact = [c for c in old_cols if c in set(new_cols)]
    key, key_warning = _detect_key(old, new, shared_exact, key_column)
    if key_warning:
        warnings.append(key_warning)

    removed_cols = [c for c in old_cols if c not in set(new_cols)]
    added_cols = [c for c in new_cols if c not in set(old_cols)]
    renames = _detect_renames(old, new, removed_cols, added_cols, key)
    rename_new_to_old = {r.new_name: r.old_name for r in renames}
    removed_final = [c for c in removed_cols if c not in {r.old_name for r in renames}]
    added_final = [c for c in added_cols if c not in set(rename_new_to_old)]

    # Column pairs compared: (old_name, new_name), in the OLD file's order.
    pairs: list[tuple[str, str]] = [(c, c) for c in shared_exact]
    pairs += [(r.old_name, r.new_name) for r in renames]
    pairs.sort(key=lambda p: old_cols.index(p[0]))
    shared_display = [p[1] for p in pairs]

    old_types, new_types = _column_types(old), _column_types(new)
    type_changes = [
        TypeChange(column=n, old_type=old_types[o], new_type=new_types[n])
        for o, n in pairs
        if old_types[o] != new_types[n]
    ]
    order_old = [p[0] for p in pairs]
    order_new_mapped = [rename_new_to_old.get(c, c) for c in new_cols if c in set(shared_display)]
    schema = SchemaDiff(
        added_columns=added_final,
        removed_columns=removed_final,
        renamed=renames,
        type_changes=type_changes,
        column_order_changed=order_old != order_new_mapped,
        shared_columns=shared_display,
    )

    # ---- normalized row hashes over shared columns (sorted by display name)
    hash_pairs = sorted(pairs, key=lambda p: p[1])

    def cells(table: CanonicalTable, row: dict, side: int) -> list[str]:
        return [normalize_cell(row.get(p[side])) for p in hash_pairs]

    old_hashes = [_row_hash(cells(old, r, 0)) for r in old.rows]
    new_hashes = [_row_hash(cells(new, r, 1)) for r in new.rows]
    old_content = hashlib.sha256("\n".join(sorted(old_hashes)).encode()).hexdigest()
    new_content = hashlib.sha256("\n".join(sorted(new_hashes)).encode()).hexdigest()
    hashes = HashSummary(
        method=(
            "sha256 over sorted per-row sha256 of whitespace-trimmed, number-normalized cells of the "
            "shared columns; independent of row order, column order and numeric formatting"
        ),
        old_content_hash=old_content,
        new_content_hash=new_content,
        identical=old_content == new_content,
    )

    # ---- row diff
    modified_by_column: Counter[str] = Counter()
    modified_rows: list[ModifiedRow] = []
    truncated = False
    if key:
        old_by_key = {(r.get(key) or "").strip(): r for r in old.rows}
        new_by_key = {(r.get(key) or "").strip(): r for r in new.rows}
        removed_keys = sorted(set(old_by_key) - set(new_by_key), key=_natural)
        added_keys = sorted(set(new_by_key) - set(old_by_key), key=_natural)
        unchanged = modified = 0
        compare_pairs = [p for p in pairs if p[1] != key]
        for k in sorted(set(old_by_key) & set(new_by_key), key=_natural):
            o_row, n_row = old_by_key[k], new_by_key[k]
            changed = [
                FieldChange(column=n, old=(o_row.get(o) or "").strip(), new=(n_row.get(n) or "").strip())
                for o, n in compare_pairs
                if normalize_cell(o_row.get(o)) != normalize_cell(n_row.get(n))
            ]
            if changed:
                modified += 1
                for ch in changed:
                    modified_by_column[ch.column] += 1
                if len(modified_rows) < max_detail_rows:
                    modified_rows.append(ModifiedRow(key=k, changed_fields=changed))
                else:
                    truncated = True
            else:
                unchanged += 1
        if len(added_keys) > max_detail_rows or len(removed_keys) > max_detail_rows:
            truncated = True
        rows = RowDiff(
            matching="by_key",
            unchanged=unchanged,
            modified=modified,
            added=len(added_keys),
            removed=len(removed_keys),
            modified_by_column=dict(modified_by_column),
            modified_rows=modified_rows,
            added_keys=added_keys[:max_detail_rows],
            removed_keys=removed_keys[:max_detail_rows],
            detail_truncated=truncated,
        )
    else:
        oc, nc = Counter(old_hashes), Counter(new_hashes)
        common = sum((oc & nc).values())
        rows = RowDiff(
            matching="by_row_hash",
            unchanged=common,
            modified=None,
            added=len(new_hashes) - common,
            removed=len(old_hashes) - common,
            modified_by_column={},
            modified_rows=[],
            added_keys=[],
            removed_keys=[],
            detail_truncated=False,
        )

    # ---- reclassify numeric type changes whose values did not change
    if rows.matching == "by_key":
        numeric = {ColumnType.INTEGER, ColumnType.FLOAT}
        for tc in schema.type_changes:
            unchanged = rows.modified_by_column.get(tc.column, 0) == 0
            tc.values_unchanged = unchanged
            if unchanged and tc.old_type in numeric and tc.new_type in numeric:
                tc.kind = "format"

    # ---- volume
    volume_pct = (
        round(100.0 * (len(new.rows) - len(old.rows)) / len(old.rows), 2) if old.rows else None
    )
    volume_flagged = volume_pct is not None and abs(volume_pct) >= volume_change_warn_pct

    # ---- null rates
    null_changes: list[NullRateChange] = []
    for o, n in pairs:
        old_rate = sum(1 for v in _values(old, o) if v == "") / len(old.rows) if old.rows else 0.0
        new_rate = sum(1 for v in _values(new, n) if v == "") / len(new.rows) if new.rows else 0.0
        delta = new_rate - old_rate
        null_changes.append(
            NullRateChange(
                column=n,
                old_rate=round(old_rate, 4),
                new_rate=round(new_rate, 4),
                delta=round(delta, 4),
                flagged=abs(delta) >= null_rate_change_warn,
            )
        )

    # ---- drift
    drift = [_drift_for_column(o, n, old, new, key, psi_warn, psi_critical) for o, n in pairs]

    # ---- findings
    findings = _build_findings(
        schema, rows, volume_pct, volume_flagged, volume_change_warn_pct,
        null_changes, null_rate_change_warn, drift, psi_warn, psi_critical, hashes,
    )

    summary = _summary(rows, schema, hashes)
    return ComparisonResult(
        old_name=old.table_name,
        new_name=new.table_name,
        old_rows=len(old.rows),
        new_rows=len(new.rows),
        volume_change_pct=volume_pct,
        volume_flagged=volume_flagged,
        key_column=key,
        schema_diff=schema,
        rows=rows,
        null_rate_changes=null_changes,
        drift=drift,
        hashes=hashes,
        findings=findings,
        summary=summary,
        warnings=warnings,
        thresholds={
            "psi_warn": psi_warn,
            "psi_critical": psi_critical,
            "null_rate_change_warn": null_rate_change_warn,
            "volume_change_warn_pct": volume_change_warn_pct,
        },
        old_profile=profile_table(old),
        new_profile=profile_table(new),
    )


def _natural(s: str):
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def _summary(rows: RowDiff, schema: SchemaDiff, hashes: HashSummary) -> str:
    if rows.matching == "by_key":
        parts = (
            f"{rows.unchanged:,} unchanged, {rows.modified:,} modified, "
            f"{rows.added:,} added, {rows.removed:,} removed"
        )
    else:
        parts = f"{rows.unchanged:,} identical rows, {rows.added:,} added, {rows.removed:,} removed (no key column)"
    schema_bits = []
    if schema.renamed:
        schema_bits.append(f"{len(schema.renamed)} renamed")
    if schema.added_columns:
        schema_bits.append(f"{len(schema.added_columns)} column(s) added")
    if schema.removed_columns:
        schema_bits.append(f"{len(schema.removed_columns)} column(s) removed")
    if schema.type_changes:
        n_fmt = sum(1 for t in schema.type_changes if t.kind == "format")
        n_type = len(schema.type_changes) - n_fmt
        if n_type:
            schema_bits.append(f"{n_type} type change(s)")
        if n_fmt:
            schema_bits.append(f"{n_fmt} format change(s), values unchanged")
    if schema.column_order_changed:
        schema_bits.append("column order changed")
    if schema_bits:
        parts += ". Schema: " + ", ".join(schema_bits)
    if hashes.identical:
        parts += ". Normalized content is identical."
    return parts


def _build_findings(
    schema: SchemaDiff,
    rows: RowDiff,
    volume_pct: float | None,
    volume_flagged: bool,
    volume_warn: float,
    null_changes: list[NullRateChange],
    null_warn: float,
    drift: list[DriftResult],
    psi_warn: float,
    psi_critical: float,
    hashes: HashSummary,
) -> list[ComparisonFinding]:
    out: list[ComparisonFinding] = []

    def add(type_, det, col, metric, observed, baseline, threshold, band, msg):
        out.append(ComparisonFinding(
            type=type_, detector_type=det, column=col, metric=metric, observed=observed,
            baseline=baseline, threshold=threshold, deviation_band=band, message=msg))

    for r in schema.renamed:
        add("column_renamed", "deterministic", r.new_name, "value_agreement", r.agreement, None,
            _RENAME_AGREEMENT_MIN, None,
            f"'{r.old_name}' looks renamed to '{r.new_name}' ({r.basis}); proposed, not certain")
    for c in schema.added_columns:
        add("column_added", "deterministic", c, "schema", "present", "absent", None, None, f"Column '{c}' was added")
    for c in schema.removed_columns:
        add("column_removed", "deterministic", c, "schema", "absent", "present", None, None, f"Column '{c}' was removed")
    for t in schema.type_changes:
        if t.kind == "format":
            add("column_format_changed", "deterministic", t.column, "inferred_type", t.new_type.value,
                t.old_type.value, None, None,
                f"'{t.column}': numeric values are now formatted differently "
                f"({t.old_type.value} -> {t.new_type.value}); values unchanged")
            continue
        add("column_type_changed", "deterministic", t.column, "inferred_type", t.new_type.value,
            t.old_type.value, None, None,
            f"'{t.column}' is now inferred as {t.new_type.value} (was {t.old_type.value})")
    if schema.column_order_changed:
        add("column_order_changed", "deterministic", None, "schema", "reordered", "original order", None, None,
            "Shared columns appear in a different order")
    if volume_pct is not None:
        add("volume_change", "statistical", None, "row_count_change_pct", volume_pct, 0.0, volume_warn,
            "warning" if volume_flagged else "stable",
            f"Row count changed {volume_pct:+.1f}%" + (" (above the heuristic threshold)" if volume_flagged else ""))
    for n in null_changes:
        if n.flagged:
            add("null_rate_change", "statistical", n.column, "null_rate_delta", n.delta, n.old_rate, null_warn,
                "warning", f"Blank rate of '{n.column}' moved from {n.old_rate:.1%} to {n.new_rate:.1%}")
    for d in drift:
        if d.value is not None and d.band in ("warning", "critical"):
            add("distribution_drift", "statistical", d.column, "psi", d.value, 0.0,
                psi_critical if d.band == "critical" else psi_warn, d.band,
                f"Distribution of '{d.column}' shifted (PSI {d.value:.3f}, {d.band})")
    if rows.matching == "by_key":
        for label, n in (("rows_added", rows.added), ("rows_removed", rows.removed), ("rows_modified", rows.modified)):
            if n:
                add(label, "integrity", None, "row_count", n, None, None, None,
                    f"{n:,} row(s) {label.split('_')[1]} between versions")
    add("content_hash", "integrity", None, "normalized_sha256",
        "identical" if hashes.identical else "different", None, None, None,
        "Normalized content is identical" if hashes.identical
        else "Normalized content differs; this proves a change, not who or why")
    return out

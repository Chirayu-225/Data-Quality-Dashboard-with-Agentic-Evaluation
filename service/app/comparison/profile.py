"""
Deterministic column profiler.

Turns a CanonicalTable into an aggregate-only profile: counts, null
rates, distinct counts, numeric summaries and a histogram, and (only
where safe) the most frequent values. It is the shared input for the
UI's column charts, for the trend strip, and for anything that later
persists a baseline.

Privacy rule baked in: a profile is meant to be storable, so it must
not carry raw PII. Frequent values ("top values") are therefore only
reported for low-cardinality columns whose name does not look personal
(email, phone, name, address, ...); everything else gets counts and
numeric aggregates only, with `top_values_withheld_reason` saying why.
Numeric min/max are kept, since they are aggregates, but a column that
is a phone number or similar is never parsed as numeric here (the shared
parser rejects those shapes).
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel

from app.canonical.models import CanonicalTable, ColumnType
from app.canonical.type_conformity import parse_numeric

# Column names that suggest personal data; top values are never reported
# for these, even when cardinality is low. Deliberately conservative:
# a false "withheld" costs a chart, a false "shown" leaks a value.
_PERSONAL_NAME_RE = re.compile(
    r"(email|e-mail|phone|mobile|name|address|ssn|tax|passport|dob|birth|iban|account)",
    re.IGNORECASE,
)
TOP_VALUES_MAX_DISTINCT = 20
TOP_VALUES_KEPT = 10
HISTOGRAM_BINS = 10
_NUMERIC_COLUMN_MIN_PARSE_RATE = 0.9


class ValueCount(BaseModel):
    value: str
    count: int


class NumericSummary(BaseModel):
    count: int
    min: float
    max: float
    mean: float
    std: float
    median: float
    p05: float
    p25: float
    p75: float
    p95: float
    # Equal-width bins between the 1st and 99th percentile, so a few
    # extreme values cannot flatten the whole chart; the values outside
    # that range are counted separately instead of dropped.
    hist_edges: list[float]
    hist_counts: list[int]
    below_range: int
    above_range: int


class ColumnProfile(BaseModel):
    name: str
    declared_type: ColumnType
    semantic_hint: str | None
    row_count: int
    null_count: int
    null_rate: float
    distinct_count: int
    numeric_parse_rate: float
    numeric: NumericSummary | None = None
    top_values: list[ValueCount] | None = None
    top_values_withheld_reason: str | None = None


class TableProfile(BaseModel):
    table_name: str
    row_count: int
    column_count: int
    columns: list[ColumnProfile]


def finite_number(value: str) -> float | None:
    """parse_numeric, but never NaN/inf (float() accepts the strings
    'nan' and 'inf', which must not count as numbers here)."""
    x = parse_numeric(value)
    if x is None or not math.isfinite(x):
        return None
    return x


def quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted, non-empty list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _numeric_summary(values: list[float]) -> NumericSummary:
    values = sorted(values)
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    lo, hi = quantile(values, 0.01), quantile(values, 0.99)
    if hi <= lo:
        edges = [lo, hi]
        counts = [sum(1 for v in values if lo <= v <= hi)]
    else:
        width = (hi - lo) / HISTOGRAM_BINS
        edges = [lo + width * i for i in range(HISTOGRAM_BINS + 1)]
        counts = [0] * HISTOGRAM_BINS
        for v in values:
            if lo <= v <= hi:
                idx = min(int((v - lo) / width), HISTOGRAM_BINS - 1)
                counts[idx] += 1
    return NumericSummary(
        count=n,
        min=values[0],
        max=values[-1],
        mean=mean,
        std=math.sqrt(var),
        median=quantile(values, 0.5),
        p05=quantile(values, 0.05),
        p25=quantile(values, 0.25),
        p75=quantile(values, 0.75),
        p95=quantile(values, 0.95),
        hist_edges=edges,
        hist_counts=counts,
        below_range=sum(1 for v in values if v < lo),
        above_range=sum(1 for v in values if v > hi),
    )


def profile_column(table: CanonicalTable, name: str) -> ColumnProfile:
    schema = next(c for c in table.columns if c.name == name)
    raw = [(row.get(name) or "").strip() for row in table.rows]
    non_blank = [v for v in raw if v != ""]
    n_rows = len(raw)
    counts = Counter(non_blank)

    numerics = [x for x in (finite_number(v) for v in non_blank) if x is not None]
    parse_rate = len(numerics) / len(non_blank) if non_blank else 0.0

    numeric = None
    if non_blank and parse_rate >= _NUMERIC_COLUMN_MIN_PARSE_RATE:
        numeric = _numeric_summary(numerics)

    top_values = None
    withheld = None
    if not non_blank:
        withheld = "no values"
    elif schema.semantic_hint in ("email", "phone") or _PERSONAL_NAME_RE.search(name):
        withheld = "column looks personal; values are never stored in a profile"
    elif len(counts) > TOP_VALUES_MAX_DISTINCT:
        withheld = f"more than {TOP_VALUES_MAX_DISTINCT} distinct values"
    else:
        top_values = [ValueCount(value=v, count=c) for v, c in counts.most_common(TOP_VALUES_KEPT)]

    return ColumnProfile(
        name=name,
        declared_type=schema.type,
        semantic_hint=schema.semantic_hint,
        row_count=n_rows,
        null_count=n_rows - len(non_blank),
        null_rate=(n_rows - len(non_blank)) / n_rows if n_rows else 0.0,
        distinct_count=len(counts),
        numeric_parse_rate=parse_rate,
        numeric=numeric,
        top_values=top_values,
        top_values_withheld_reason=withheld,
    )


def profile_table(table: CanonicalTable) -> TableProfile:
    return TableProfile(
        table_name=table.table_name,
        row_count=len(table.rows),
        column_count=len(table.columns),
        columns=[profile_column(table, c.name) for c in table.columns],
    )

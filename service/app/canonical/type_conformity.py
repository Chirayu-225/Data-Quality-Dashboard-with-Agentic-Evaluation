"""
Shared value-parsing logic for type inference and type-consistency
checking — ONE place that decides "does this string look like an
integer / a float / a boolean", used by both `csv_connector._infer_type`
(deciding what a column's type IS) and `TypeConsistencyCheck`/`OutlierCheck`
(deciding whether a given row's value actually CONFORMS to that type).
Before this module existed, those were three separate ad hoc parsing
spots that could silently disagree with each other (e.g. OutlierCheck
calling bare `float(v)` and skipping anything that didn't parse, while
inference used its own separate int()/float() attempts) — this is what
keeps "the column's type" and "does this value match it" answering the
same question the same way everywhere.

Covers the known false-positive traps a naive int()/float() parse walks
straight into:
- Leading zeros (zip codes, IDs like "02134") must NOT be treated as a
  plain integer — losing the leading zero on round-trip means it isn't
  really being read back as the same value.
- Currency symbols and thousands separators ("$1,200") are legitimate
  numeric formatting, not a type violation.
- Scientific notation ("1.5e10") is already handled correctly by
  Python's own `float()` — no special-casing needed there.
- Percentages ("45%") are a numeric value with a trailing marker.
- Boolean variants beyond true/false (yes/no, y/n) should be recognized
  as boolean WITHOUT also claiming "1"/"0" are boolean — those are
  genuinely ambiguous with an ordinary integer flag column, so they are
  deliberately left alone (see `_BOOLEAN_TOKENS` below).
"""

from __future__ import annotations

import re

# Values that mean "missing data", not "a value of the wrong type" —
# normalized to blank at ingest (see csv_connector.py) so completeness
# catches them naturally, instead of type inference being fooled by
# them into an unhelpful UNKNOWN/STRING-only column. Case-insensitive,
# whitespace-trimmed; configurable per connector call.
DEFAULT_NULL_SENTINELS: frozenset[str] = frozenset(
    {"n/a", "na", "null", "none", "nil", "-", "#n/a", "nan"}
)

_CURRENCY_CHARS = "$€£"

# Deliberately NOT "1"/"0" here — those are ambiguous with a genuine
# integer flag column (0/1), and silently reclassifying them as
# boolean would be a surprising, unrequested type change. A column of
# pure "1"/"0" stays INTEGER, exactly as before this module existed.
_BOOLEAN_TOKENS: dict[str, bool] = {
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "y": True,
    "n": False,
}

# A plain integer: optional sign, then either "0" or a digit string with
# no leading zero. This is what rejects "02134" — int("02134") == 2134,
# but "2134" != "02134", so a leading-zero numeric string never conforms
# here and the column is left to be classified/validated as a string
# instead of silently losing information.
_INTEGER_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")

# A leading-zero, all-digit value with no decimal point or exponent —
# "02134", "00501" — is the zip-code/ID trap: float() happily parses it
# (losing the leading zero, same as int() does), but that's exactly the
# kind of silent information loss this module exists to avoid. This is
# checked by parse_numeric too, not just parse_integer_strict, so a
# zip-code-shaped column doesn't sneak into FLOAT just because the
# stricter integer parse correctly rejected it first.
_LEADING_ZERO_INTEGER_RE = re.compile(r"^[+-]?0\d+$")

# Common date/datetime SHAPES, not a real calendar-aware parse — this is
# deliberately a cheap heuristic (same spirit as parse_numeric/
# parse_boolean above), used only to decide whether a column "leans
# date" for null-sentinel gating (see csv_connector.py's docstring on
# why that gating exists). Covers ISO 8601 (2024-01-31, with an optional
# time part), and the common US/EU slash/dot-separated forms
# (01/31/2024, 31/01/2024, 31.01.2024). A column of real dates in one of
# these shapes is overwhelmingly the common case; anything stranger
# simply doesn't count as "date-leaning" here; that's a conservative
# (not aggressive) side to be wrong on for this specific decision.
_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$"
    r"|^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$"
)


def looks_like_date(value: str) -> bool:
    """Cheap shape-only check — see `_DATE_RE`'s docstring for exactly
    what it does and doesn't cover, and why that's the right amount of
    effort for this particular decision."""
    return bool(_DATE_RE.match(value.strip()))


def is_null_sentinel(value: str, sentinels: frozenset[str] | set[str] | None = None) -> bool:
    """True if `value` is one of a configurable set of strings that mean
    "missing", not "wrong type" — e.g. "N/A", "null", "-". The caller is
    expected to have already excluded genuinely blank strings; this is
    specifically about non-blank sentinel text."""
    if not value:
        return False
    table = sentinels if sentinels is not None else DEFAULT_NULL_SENTINELS
    return value.strip().lower() in table


def parse_boolean(value: str) -> bool | None:
    """Strict — ALL non-blank values in a column must match for the
    column to be inferred BOOLEAN (see csv_connector._infer_type).
    Recognizes true/false and yes/no/y/n, case-insensitively. See
    `_BOOLEAN_TOKENS` above for why "1"/"0" are deliberately excluded."""
    return _BOOLEAN_TOKENS.get(value.strip().lower())


def parse_integer_strict(value: str) -> int | None:
    """Succeeds only when the string round-trips exactly through int() —
    this is what keeps a zip code or ID with a leading zero ("02134")
    OUT of integer classification, while still accepting "0" and signed
    integers ("-5", "+5") normally. Used both for type inference (does
    this column conform enough to call it INTEGER) and for
    TypeConsistencyCheck (does this one row's value conform)."""
    s = value.strip()
    if not _INTEGER_RE.match(s):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_numeric(value: str) -> float | None:
    """Looser than `parse_integer_strict` — used for FLOAT inference, for
    TypeConsistencyCheck on a FLOAT column, and for OutlierCheck, none of
    which care whether a value's formatting round-trips, only whether it
    IS a number. Strips a single leading currency symbol, thousands-
    separator commas, and a trailing '%' before parsing. Python's own
    `float()` already parses scientific notation ("1.5e10") correctly, so
    there's nothing extra to do for that case."""
    s = value.strip()
    if not s:
        return None
    if s[0] in _CURRENCY_CHARS:
        s = s[1:]
    s = s.replace(",", "")
    if s.endswith("%"):
        s = s[:-1]
    if not s:
        return None
    if _LEADING_ZERO_INTEGER_RE.match(s):
        return None  # zip-code/ID trap — see _LEADING_ZERO_INTEGER_RE above
    try:
        return float(s)
    except ValueError:
        return None

"""
Local value review — the semantic layer's default tier. No LLM, no
network, no data leaves the process.

Why this exists: most of what the semantic layer is asked to catch is a
judgment about a VALUE, not a row. "asdfgh" is an implausible company
name wherever it appears; a department name sitting in a first-name
column is wrong in any row. Judging distinct values per column, locally,
removes both problems the row-by-row LLM design has: raw rows no longer
go to an external provider, and cost no longer scales with row count
(work is per DISTINCT value, and the scoring here is in-process).

What it does:
1. Decide which columns are worth reviewing (free text, ids, numbers,
   emails, phones and dates are skipped and the skip reason is
   reported).
2. Reduce each reviewed column to its distinct values with counts.
3. Score each distinct value with independent signals (keyboard mash,
   placeholder words, record-status markers, role/system words in a
   person-name column, repeated tokens, shape that deviates from the
   rest of the column).
4. Map every finding back to all the rows holding that value and emit
   the same `SemanticFlag` shape the LLM path emits, so downstream
   scoring and display need no changes.

Honest limits, stated so nobody over-reads the output:
- It cannot judge anything that needs world knowledge ("is this a real
  company?"). A fluent made-up name passes.
- The word lists below are generic heuristics, not a model. They were
  written from general knowledge of placeholder/test data and tuned
  against nothing; measure them on data you did not write them from.
- Signals are independent heuristics, not independent evidence in the
  statistical sense. Confidence combines them; it is not a calibrated
  probability.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

from pydantic import BaseModel

from app.agent.semantic_reasoning import SemanticFlag
from app.canonical.models import CanonicalTable, ColumnType

MIN_ROWS_TO_REVIEW = 5
# A value this common in its column is "a normal value many rows share",
# never an anomaly (mirrors the LLM prompt's own rule).
_COMMON_VALUE_SHARE = 0.02
_COMMON_VALUE_MIN_COUNT = 3

_FREE_TEXT_NAME_TOKENS = {
    "note", "notes", "comment", "comments", "description", "desc",
    "remark", "remarks", "message", "memo", "text", "body", "summary",
}
_ID_NAME_RE = re.compile(r"(^|_)(id|uuid|guid|key|code|zip|postal|postcode|pin)$", re.I)
_PERSON_NAME_RE = re.compile(
    r"(^|_)(first|last|middle|full|given|sur|family)?_?(name|fname|lname|surname)$", re.I
)
_SKIP_HINTS = {"email", "phone", "date"}

_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Generic placeholder / test-data vocabulary. Whole-token matches only.
_PLACEHOLDER_TOKENS = {
    "test", "testing", "tester", "dummy", "placeholder", "lorem", "ipsum",
    "foobar", "asdf", "qwerty", "xxx", "xxxx", "tbd", "unknown", "temporary",
}
# Also real surnames/words (Bar, Foo, Sample, Temp, Demo, Fake), so only
# count when the value has two or more words.
_PLACEHOLDER_AMBIGUOUS = {"foo", "bar", "sample", "temp", "demo", "fake", "example"}
# Record-status markers that mean "this record is not real/usable".
_STATUS_MARKERS = {
    "obsolete", "deleted", "void", "voided", "duplicate", "inactive",
    "cancelled", "canceled", "archived", "deprecated", "invalid",
}
_STATUS_PHRASES = ("do not use", "dont use", "don't use", "do not call", "do not contact", "not in use")
# Words that describe a department, role or system account, which
# should not be the value of a PERSON-name column.
# Unmistakable even as a single word.
_ROLE_TOKENS_STRICT = {
    "dept", "department", "payable", "payables", "receivable", "receivables",
    "invoice", "invoices", "admin", "administrator", "support", "billing",
    "accounting", "info", "noreply", "helpdesk", "payroll", "procurement",
}
# Real surnames/words too ("Sales", "Service"), so only count when the
# value has two or more words ("Sales Team", "Wire Transfer Dept").
_ROLE_TOKENS_AMBIGUOUS = {
    "team", "accounts", "sales", "service", "services", "office", "hr",
    "finance", "transfer", "wire", "reception", "operations", "customer",
}

# Unicode-aware: a word is a run of letters/combining marks in ANY script
# ("Baró" is one word, never "Bar" + "ó"; Devanagari/Hangul/Han all count).
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_NAME_CHARS_EXTRA = " .'\u2019\u02bb-"


def _is_wordlike(v: str) -> bool:
    """True when every character is a letter or combining mark in any
    script, or one of space . ' - (so O'Brien, Nguyen-Smith, de la Cruz
    and 张伟 all count as plain text values)."""
    if not v:
        return False
    first = unicodedata.category(v[0])
    if not first.startswith("L"):
        return False
    return all(unicodedata.category(ch)[0] in ("L", "M") or ch in _NAME_CHARS_EXTRA for ch in v)


class ValueFinding(BaseModel):
    column: str
    value: str
    row_indices: list[int]
    reason: str
    confidence: float
    signals: list[str]


class LocalReviewResult(BaseModel):
    flags: list[SemanticFlag]
    findings: list[ValueFinding]
    columns_reviewed: list[str]
    columns_skipped: dict[str, str]
    distinct_values_scored: int
    rows_considered: int
    # Always 0 here — recorded so the egress ledger can state it.
    values_sent_externally: int = 0


# Keyboard-row runs need 5+ characters: a 4-character run ("erty") sits
# inside ordinary surnames and words (Liberty, Haverty, Doherty), found by
# testing on 6,000 generated multi-locale rows. Alphabet runs need 5 too (4 hits "Karstula": r-s-t-u).
_KEYBOARD_RUN = 5
_ALPHABET_RUN = 5


def _has_keyboard_run(token: str) -> bool:
    t = token.lower()
    for row in _KEYBOARD_ROWS:
        for seq in (row, row[::-1]):
            for i in range(len(seq) - _KEYBOARD_RUN + 1):
                if seq[i : i + _KEYBOARD_RUN] in t:
                    return True
    for seq in (_ALPHABET, _ALPHABET[::-1]):
        for i in range(len(seq) - _ALPHABET_RUN + 1):
            if seq[i : i + _ALPHABET_RUN] in t:
                return True
    return False


def _has_char_run(token: str, min_run: int = 4) -> bool:
    return re.search(r"(.)\1{%d,}" % (min_run - 1), token) is not None


def _is_sequential_digits(value: str) -> bool:
    if len(value) < 5 or not value.isdigit():
        return False
    diffs = {int(b) - int(a) for a, b in zip(value, value[1:])}
    return diffs in ({1}, {-1})


def _column_skip_reason(table: CanonicalTable, name: str, hint: str | None, ctype: ColumnType,
                        values: list[str]) -> str | None:
    lname = name.lower()
    if hint in _SKIP_HINTS:
        return f"{hint} column (checked by format rules)"
    if ctype in (ColumnType.INTEGER, ColumnType.FLOAT, ColumnType.BOOLEAN, ColumnType.DATE, ColumnType.DATETIME):
        return f"{ctype.value} column (checked by numeric/type rules)"
    tokens = set(re.split(r"[^a-z0-9]+", lname))
    if tokens & _FREE_TEXT_NAME_TOKENS:
        return "free-text column (skipped: free text is not judged value-by-value)"
    if len(values) < MIN_ROWS_TO_REVIEW:
        return "too few filled values"
    avg_len = sum(len(v) for v in values) / len(values)
    if avg_len > 30:
        return "long text column (skipped as free text)"
    if _ID_NAME_RE.search(lname) and all(v.isdigit() for v in values if v):
        # numeric-looking identifiers and codes: only the shape signals
        # that make sense for digits are applied, and those need a
        # majority-alpha column; nothing to review here.
        return "identifier column"
    return None


def _column_profile(values_counter: Counter[str]) -> dict:
    total = sum(values_counter.values())
    alpha = sum(c for v, c in values_counter.items() if _is_wordlike(v))
    return {"total": total, "alpha_share": alpha / total if total else 0.0}


def _score_value(value: str, profile: dict, is_person_name: bool) -> list[tuple[str, float, str]]:
    """Returns [(signal_name, weight, human reason)]. Precision is
    favoured over recall: a false flag on a real customer name costs
    trust, and it lands hardest on names that look unusual to an
    English-trained eye."""
    out: list[tuple[str, float, str]] = []
    v = value.strip()
    low = v.lower()
    words = [w.lower() for w in _WORD_RE.findall(v)]
    n_words = len(words)

    if len(v) >= 4 and (_has_char_run(v) or any(_has_keyboard_run(w) for w in words)):
        out.append(("keyboard_mash", 0.9, f"'{v}' looks like keyboard mashing or a repeated character"))
    if _is_sequential_digits(v):
        out.append(("digit_sequence", 0.8, f"'{v}' is a sequential-digit placeholder"))
    if len(set(v)) == 1 and len(v) >= 4 and v.isdigit():
        out.append(("repeated_digits", 0.85, f"'{v}' is a repeated-digit placeholder"))
    if words and (
        any(w in _PLACEHOLDER_TOKENS for w in words)
        or (n_words >= 2 and any(w in _PLACEHOLDER_AMBIGUOUS for w in words))
    ):
        out.append(("placeholder_word", 0.85, f"'{v}' contains a placeholder/test word"))
    if (words and any(w in _STATUS_MARKERS for w in words)) or any(p in low for p in _STATUS_PHRASES):
        out.append(("status_marker", 0.85, f"'{v}' reads as a record-status marker, not a real value"))
    tokens = low.split()
    if len(tokens) >= 2 and len(set(tokens)) == 1:
        # Two identical parts occur in real names ("Mei Mei") and
        # brands ("Tom Tom"); three or more is a placeholder.
        w = 0.85 if len(tokens) >= 3 else 0.5
        out.append(("repeated_token", w, f"'{v}' is the same word repeated"))
    if is_person_name and words and (
        any(w in _ROLE_TOKENS_STRICT for w in words)
        or (n_words >= 2 and any(w in _ROLE_TOKENS_AMBIGUOUS for w in words))
    ):
        out.append(("role_in_person_name", 0.8, f"'{v}' looks like a department/role/system name in a person-name column"))

    # Shape deviating from a column that is overwhelmingly plain text.
    if profile["alpha_share"] >= 0.90 and not _is_wordlike(v):
        has_letter = any(unicodedata.category(ch)[0] == "L" for ch in v)
        has_digit = any(ch.isdigit() for ch in v)
        if v.isdigit():
            out.append(("digits_in_text_column", 0.85, f"'{v}' is all digits in a column of text values"))
        elif has_letter and has_digit:
            # Digits in a company name are common (3M, 7-Eleven); in a
            # person's name they are not.
            w = 0.7 if is_person_name else 0.5
            out.append(("digit_inside_word", w, f"'{v}' mixes digits into a word in a column of plain text values"))
        elif re.search(r"[#@$%^*=<>{}\[\]|\\~`;]", v) or (is_person_name and re.search(r"[&/+]", v)):
            out.append(("symbol_in_text_column", 0.7, f"'{v}' contains symbols unusual for this column"))
    return out


def _combine(weights: list[float]) -> float:
    p = 1.0
    for w in weights:
        p *= 1.0 - w
    return round(1.0 - p, 2)


def review_table_locally(
    table: CanonicalTable,
    *,
    exclude_indices: set[int] | None = None,
    min_confidence: float = 0.6,
) -> LocalReviewResult:
    exclude = exclude_indices or set()
    n_rows = len(table.rows)
    rows_considered = n_rows - len(exclude)
    skipped: dict[str, str] = {}
    reviewed: list[str] = []
    findings: list[ValueFinding] = []
    distinct_scored = 0

    for col in table.columns:
        name = col.name
        by_value: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(table.rows):
            if i in exclude:
                continue
            raw = row.get(name)
            if raw is None:
                continue
            s = str(raw).strip()
            if s:
                by_value[s].append(i)
        all_values = [s for s, idx in by_value.items() for _ in idx]
        reason = _column_skip_reason(table, name, col.semantic_hint, col.type, all_values)
        if reason:
            skipped[name] = reason
            continue

        reviewed.append(name)
        counter: Counter[str] = Counter({s: len(idx) for s, idx in by_value.items()})
        profile = _column_profile(counter)
        is_person = bool(_PERSON_NAME_RE.search(name.lower()))
        common_cut = max(_COMMON_VALUE_MIN_COUNT, int(_COMMON_VALUE_SHARE * profile["total"]))

        for value, idx in by_value.items():
            distinct_scored += 1
            count = len(idx)
            if count >= common_cut:
                continue
            signals = _score_value(value, profile, is_person)
            if not signals:
                continue
            conf = _combine([w for _, w, _ in signals])
            if conf < min_confidence:
                continue
            findings.append(
                ValueFinding(
                    column=name,
                    value=value,
                    row_indices=sorted(idx),
                    reason=f"{name}: " + "; ".join(r for _, _, r in signals),
                    confidence=conf,
                    signals=[s for s, _, _ in signals],
                )
            )

    # One flag per row (merge multiple columns on the same row).
    per_row: dict[int, list[ValueFinding]] = defaultdict(list)
    for f in findings:
        for i in f.row_indices:
            per_row[i].append(f)
    flags = [
        SemanticFlag(
            row_index=i,
            reason=" | ".join(f.reason for f in fs),
            confidence=_combine([f.confidence for f in fs]),
        )
        for i, fs in sorted(per_row.items())
    ]
    return LocalReviewResult(
        flags=flags,
        findings=findings,
        columns_reviewed=reviewed,
        columns_skipped=skipped,
        distinct_values_scored=distinct_scored,
        rows_considered=rows_considered,
    )

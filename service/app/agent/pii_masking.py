"""
PII masking for data sent to third-party LLM providers (Groq, Gemini).

This exists because of a product-level consent decision: semantic
reasoning (and everything downstream of it — verification, cross-table
reasoning) sends row content to two external APIs. Even with explicit
per-scan consent (see main.py's `ai_review_consent` gate), the two PII
categories the system already knows how to recognize by name — email
and phone, via CSVConnector._infer_semantic_hint's `semantic_hint` —
are masked unconditionally before any row reaches a prompt. Consent
controls WHETHER data leaves the server at all; masking controls WHAT
leaves it, and the two are independent, deliberately: a user who
consents to AI review still shouldn't have to also trust that nothing
else goes wrong, so email/phone values never go out in the clear
regardless of consent.

Only applies to the LLM-facing path. Deterministic checks (format
validation, duplicate detection, etc.) and the final report shown back
to the user both still operate on — and show — the REAL row values;
only the copy actually serialized into a prompt string is masked. See
GeneratorAgent.run: deterministic checks run against the real `table`
object before any masked copy is ever built, and the final
ScanResponse's `flagged_rows`/`security_findings` (main.py) are built
from the real table too, never the masked one.

Two layers of masking, both value-keyed (see below), not row-keyed:

1. STRUCTURAL — a whole-cell replacement for a column the connector
   recognizes as email/phone by name (`semantic_hint`), or that the
   caller named via `exclude_columns` (e.g. a name column the connector
   has no hint for). The entire cell value is replaced.
2. EMBEDDED — a regex scrub applied to every OTHER string-valued
   column (free text — a `notes` field, a `description`, an unhinted
   `mobile`/`tel`/`contact` column the name-based heuristic missed).
   Only the matched email/phone-SHAPED substring inside the cell is
   replaced; the rest of the text is left alone. This is a cheap,
   deliberately imperfect backstop for exactly the gap structural
   masking can't close on its own — PII in a column nothing hinted
   as PII at all.

CORRECTED DESIGN (this module previously keyed placeholders by ROW
INDEX, not by value — e.g. the value in row 7's email column became
"EMAIL_7" regardless of what it actually was). That was a real defect,
not a style choice: it meant the exact same email appearing in rows 7
and 12 became two DIFFERENT placeholders, destroying the semantic
layer's ability to notice "these two records share a contact" — one of
the strongest duplicate/fraud signals in CRM data, and exactly the kind
of judgment call semantic reasoning exists to make. Every placeholder
here is now a function of the REAL VALUE (lowercased, so case
differences still correlate) plus a per-scan salt — so the SAME value
anywhere in a scan (any column, any row, either side of a cross-table
pair) always produces the SAME token, while a different value never
collides with it by construction (a hash, not a counter).

The salt is generated ONCE per scan request (main.py's
`secrets.token_hex`, threaded down through `GeneratorAgent.run` and the
cross-table pass as `mask_salt`) and is NOT derived from the data or
any fixed secret — so a token from one scan carries no meaning in a
different scan, and the real value can't be recovered from the token
without that scan's own salt (which never leaves the server). This is
what "ideally via a salted hash so tokens can't be reversed across
scans" means in practice: correlation works WITHIN one scan (the
actual thing a duplicate/fraud signal needs), never ACROSS scans or
across a provider's own logs from different requests.

What this module deliberately does NOT mask by default: a name column
with no semantic hint and not explicitly listed in `exclude_columns`
goes to the LLM as-is — some semantic checks (duplicate-by-name,
"does this name match the account" cross-table judgment) genuinely
need real names to do their job, and masking every human name by
default would quietly break those checks for everyone. This is a
deliberate scope choice, not an oversight — see docs/DATA_HANDLING.md,
which states it plainly rather than leaving it implicit. A caller who
wants names masked anyway still can, via `exclude_columns`.

A blank/null value is left blank rather than masked — manufacturing a
fake non-null placeholder where the real cell was empty could itself
mislead a completeness-adjacent judgment call the LLM makes in
passing, and there's nothing sensitive in a blank cell to protect in
the first place.
"""

from __future__ import annotations

import hashlib
import re

from app.canonical.models import CanonicalTable

# Appended to every system prompt that sees masked rows. Found by the first
# live 300-row run: with no explanation, the generator read tokens such as
# EMAIL_c1fe8eadaa as "placeholder emails" and flagged 37 ordinary rows, and
# the verifier (which sees the same tokens) confirmed them. Both models must
# be told the tokens are our own privacy masking, not data.
MASKING_NOTE = (
    "\n\nPRIVACY MASKING: to protect personal data, real emails and phone numbers "
    "have been replaced with tokens such as EMAIL_1a2b3c4d5e or PHONE_1a2b3c4d5e. "
    "These tokens are NOT part of the data and are NOT a defect: never flag a row "
    "because a value looks like such a token, a placeholder, or an invalid email or "
    "phone number. The same token always stands for the same original value, so two "
    "rows with the same token share that value. A genuinely EMPTY field is still "
    "empty, and text that is not a token (for example 'N/A' or 'test@test') is real "
    "data you may judge."
)

# Keyed by ColumnSchema.semantic_hint (see csv_connector.py's
# _infer_semantic_hint) — the two PII categories this system already
# recognizes by name, masked unconditionally. "date" is a recognized
# hint too but isn't PII on its own and is deliberately left out here.
_AUTO_MASKED_HINTS: dict[str, str] = {
    "email": "EMAIL",
    "phone": "PHONE",
}

# Backstop patterns for PII embedded inside a column nothing hinted as
# PII at all — a free-text `notes` field, or a column name the
# name-based heuristic (csv_connector.py's _infer_semantic_hint) missed
# (`mobile`, `tel`, `contact`, ...). Deliberately cheap and imperfect,
# not a full PII-detection model: this is a backstop for the common
# case, not a guarantee every possible PII shape is caught.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Candidate finder only — a candidate is NOT masked until
# `_is_plausible_phone` accepts it. The lookarounds also reject a
# candidate glued to letters/digits/hyphens on either side, so the tail
# of an order or invoice id ("ORD-2024-001234") is never mistaken for a
# phone number.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w-])(\+?\d[\d\-.\s()]{5,22}\d)(?![\w-])")

# Shapes that look phone-ish to a digit counter but aren't.
_DATE_PREFIX_RE = re.compile(
    r"^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\d)"
)
_DOTTED_ONLY_RE = re.compile(r"^\+?\d+(\.\d+)+$")
# The one dotted layout that IS a phone: 555.123.4567 / 1.555.123.4567.
_DOTTED_PHONE_RE = re.compile(r"^(\d{1,3}\.)?\d{3}\.\d{3}\.\d{4}$")
_EXTENSION_RE = re.compile(r"(?i)\s*(?:x|ext\.?)\s*\d+\s*$")

_MIN_PHONE_DIGITS = 7
_MAX_PHONE_DIGITS = 15  # E.164's own ceiling


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _is_plausible_phone(candidate: str) -> bool:
    """Phone-shaped, not merely digit-shaped. Requires 7-15 digits and
    rejects dates, IPv4 addresses, dotted version numbers, and
    order/invoice ids ("1234-5678-90", "20240115-0042"). Deliberately
    errs toward masking when a shape is genuinely ambiguous: a missed
    phone number is a privacy leak, a masked order id is only a small
    loss of context for the LLM."""
    digits = re.sub(r"\D", "", candidate)
    if not (_MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS):
        return False
    if _DATE_PREFIX_RE.match(candidate.strip()):
        return False
    stripped = candidate.strip()
    has_plus = stripped.startswith("+")
    if _DOTTED_ONLY_RE.match(stripped):
        return bool(_DOTTED_PHONE_RE.match(stripped.lstrip("+")))
    if has_plus:
        return True  # explicit country-code marker: trust the digit count
    has_paren = "(" in stripped
    if not has_paren and not re.search(r"[\-\s.]", stripped):
        return False  # a bare digit run (order id, SKU, account number)
    groups = re.findall(r"\d+", stripped)
    if len(groups) >= 2 and not has_paren:
        # Phone layouts keep every non-final group short (<= 5 digits);
        # "20240115-0042" and "1234-5678-90" fail this or the next test.
        if any(len(g) > 5 for g in groups[:-1]):
            return False
        # A real final group is >= 3 digits, except the French-style
        # all-pairs layout ("01 23 45 67 89").
        if len(groups[-1]) < 3 and not all(len(g) == 2 for g in groups[1:]):
            return False
    return True


def normalize_phone(value: str) -> str:
    """Canonical form used for hashing, so "+1 (555) 123-4567",
    "555-123-4567", "1.555.123.4567" and "5551234567" all produce the
    SAME token — formatting varies most in phone fields, and duplicate
    detection across differently-formatted numbers is exactly the signal
    value-keyed masking exists to preserve. Drops any extension, keeps
    digits only, then compares on the last 10 digits when there are at
    least 10 (so a country code, "+1"/"+44", or a national trunk "0"
    never splits one number into two tokens). Trade-off: two numbers in
    different countries sharing their last 10 digits would collide —
    vanishingly rare, and the cost is only one over-correlated pair."""
    digits = re.sub(r"\D", "", _EXTENSION_RE.sub("", value))
    return digits[-10:] if len(digits) >= 10 else digits


def _token(label: str, value: str, salt: str) -> str:
    """A stable placeholder for `value`, scoped to one scan via `salt`.
    sha256(salt + normalized value) means: the SAME real value anywhere
    in this scan always yields the SAME token (duplicate/fraud
    correlation preserved); a DIFFERENT value gets a different token by
    construction; and nothing about the token lets a reader recover the
    real value or correlate it with a DIFFERENT scan's salt.
    Normalization before hashing: emails/generic text are
    trimmed+lowercased; phones go through `normalize_phone`."""
    normalized = normalize_phone(value) if label == "PHONE" else value.strip().lower()
    digest = hashlib.sha256(f"{salt}:{normalized}".encode("utf-8", errors="replace"))
    return f"{label}_{digest.hexdigest()[:10]}"


def _placeholder_label(column_name: str, hint: str | None) -> str:
    label = _AUTO_MASKED_HINTS.get(hint or "")
    if label:
        return label
    # A caller-specified exclusion with no recognized hint — derive a
    # readable placeholder from the column's own name rather than a
    # generic one, so a flag reason mentioning "FIRST_NAME_xxxxxxxxxx"
    # is still legible to whoever reads it later.
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in column_name.strip().upper())
    return cleaned.strip("_") or "FIELD"


def columns_to_mask(
    table: CanonicalTable, extra_exclude_columns: set[str] | None = None
) -> dict[str, str | None]:
    """{column_name: semantic_hint-or-None} for every column that gets
    STRUCTURAL (whole-cell) masking before any row from `table` reaches
    an LLM prompt — the automatic email/phone columns plus any
    caller-specified exclusions that actually exist on this table.
    Every OTHER string column still gets the EMBEDDED regex scrub (see
    `_scrub_embedded_pii`) — this function only governs the stronger,
    whole-cell layer. A caller-specified name that doesn't match any
    real column is silently ignored here (main.py is responsible for
    warning about an unknown column name, the same convention
    `required_columns` already follows)."""
    extra = {c.strip() for c in (extra_exclude_columns or set()) if c.strip()}
    result: dict[str, str | None] = {}
    for c in table.columns:
        if c.semantic_hint in _AUTO_MASKED_HINTS:
            result[c.name] = c.semantic_hint
        elif c.name in extra:
            result[c.name] = None
    return result


def _scrub_phones(value: str, salt: str) -> str:
    def _replace(m: re.Match) -> str:
        candidate = m.group(0)
        date = _DATE_PREFIX_RE.match(candidate)
        if date:
            # "2024-01-15 555-123-4567": keep the date, rescan the rest.
            return candidate[: date.end()] + _scrub_phones(candidate[date.end():], salt)
        if not _is_plausible_phone(candidate):
            return candidate
        return _token("PHONE", candidate, salt)

    return _PHONE_CANDIDATE_RE.sub(_replace, value)


def _scrub_embedded_pii(value: str, salt: str) -> str:
    """Replaces email- and phone-SHAPED substrings inside free text
    with the same value-keyed token `_token` would produce for that
    exact substring — so an email that appears both in a structured
    `email` column (masked wholesale, above) and quoted inside a
    `notes` field maps to the IDENTICAL token in both places, and the
    correlation signal survives even when the same PII leaks into an
    unhinted column. Dates, IP addresses, version numbers and order ids
    are deliberately left alone — see `_is_plausible_phone` and the
    near-miss corpus in tests/test_pii_masking.py."""
    value = _EMAIL_RE.sub(lambda m: _token("EMAIL", m.group(0), salt), value)
    return _scrub_phones(value, salt)


def mask_table_for_llm(
    table: CanonicalTable,
    extra_exclude_columns: set[str] | None = None,
    salt: str = "",
) -> CanonicalTable:
    """Returns a NEW CanonicalTable — same columns/metadata, every
    masked value replaced by a token stable for that exact value within
    this scan (see `_token`'s docstring for what "stable" means and
    why). Never mutates `table` itself: deterministic checks and the
    final report both still need the real values, and this is only
    ever meant to be called on a throwaway copy built specifically for
    one LLM-facing call.

    `salt` should be the SAME value for every table and every pass
    (single-table semantic reasoning, verification, cross-table
    reasoning) within one scan request — main.py generates it once per
    request and threads it through, specifically so an email shared
    between a child row and its linked parent row still correlates
    across the cross-table pass too. An empty/default salt still works
    (deterministic within that single call) but gives up the
    cross-call correlation that's the whole point — callers should
    always pass a real per-request salt in production use.

    Returns `table` itself, unchanged, when nothing on it needs
    masking AND no column has any string value to scrub — the common
    case for a table with no PII-hinted or caller-excluded columns and
    no free text, so callers never pay for a pointless copy."""
    structural = columns_to_mask(table, extra_exclude_columns)
    # A date-hinted column is excluded from the embedded scrub too, not
    # just structural masking: a plain "2024-01-01"-shaped date string
    # has enough digits-plus-separators to false-positive match the
    # phone pattern below, and a date is not PII in the first place.
    scrub_columns = [
        c.name
        for c in table.columns
        if c.name not in structural
        and c.type.value == "string"
        and c.semantic_hint != "date"
    ]
    if not structural and not scrub_columns:
        return table

    masked_rows: list[dict] = []
    for row in table.rows:
        new_row = dict(row)
        for col_name, hint in structural.items():
            if col_name not in new_row or _is_blank(new_row[col_name]):
                continue
            new_row[col_name] = _token(_placeholder_label(col_name, hint), str(new_row[col_name]), salt)
        for col_name in scrub_columns:
            value = new_row.get(col_name)
            if _is_blank(value):
                continue
            new_row[col_name] = _scrub_embedded_pii(str(value), salt)
        masked_rows.append(new_row)

    return table.model_copy(update={"rows": masked_rows})

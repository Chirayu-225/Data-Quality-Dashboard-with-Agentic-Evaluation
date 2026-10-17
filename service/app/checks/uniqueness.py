from __future__ import annotations

import re
from collections import defaultdict

from app.canonical.models import CanonicalTable, ScanContext
from app.checks.base import Check, CheckResult, MetricCategory, register

# Same "hints over hardcoding" pattern this codebase already uses
# elsewhere (CSVConnector._infer_semantic_hint tags a column "email" by
# name substring, not a fixed literal field list; ReferentialIntegrityCheck
# does the same with the "_id" suffix) — applied here to identify which
# columns represent entity identity, rather than a fixed list of exact
# CRM field names. A column only needs to MATCH one of these common
# names, not be named exactly this; see _name_and_company_columns.
_NAME_COLUMN_NAMES = {"name", "full_name", "contact_name", "customer_name"}
_COMPANY_COLUMN_NAMES = {"company", "company_name", "account_name", "organization", "org"}
# A person's name is very often split across two columns (first_name +
# last_name) instead of one `name` column — the exact shape of the
# leads_300 test file, where this gap meant the name+company key never
# ran at all. A pair is only used when BOTH halves exist.
_FIRST_NAME_COLUMN_NAMES = ("first_name", "firstname", "first name", "given_name")
_LAST_NAME_COLUMN_NAMES = ("last_name", "lastname", "last name", "surname", "family_name")

# Shared/role mailbox local-parts (the part before "@") — a code review
# flagged these as a real false-positive risk: "info@acme.com" or
# "sales@acme.com" legitimately receives mail on behalf of, and is
# reused by, multiple different real contacts at the same company. That
# makes a shared role address the OPPOSITE of a reliable identity
# signal (the entire premise `_email_column`-based matching rests on),
# so these are excluded from email-key grouping entirely — same
# treatment as a blank email, not a lower-confidence match, since there
# is no way to tell apart "same person" from "different people sharing
# an inbox" from the row data alone.
_ROLE_LOCAL_PARTS = {
    "info", "sales", "support", "contact", "admin", "help", "hello",
    "billing", "careers", "hr", "office", "team", "noreply", "no-reply",
    "webmaster", "postmaster", "marketing", "orders", "service",
}

# A name+company match backed by only 2 rows is a strong signal (the
# same real-world person's name and employer, both matching). Once a
# single normalized name+company key groups 3 or more rows, common
# names at large employers become a real risk ("John Smith" at a
# company with hundreds of Johns) — still worth flagging, but as a
# LOWER-CONFIDENCE match (row_severity="low") rather than a certain
# one, rather than either silently dropping it or reporting it exactly
# like a 2-row match.
_LARGE_GROUP_LOW_CONFIDENCE_THRESHOLD = 3


def _normalize(value: object) -> str:
    """Lowercase, strip, and collapse internal whitespace runs to one
    space — case differences and stray/doubled whitespace shouldn't
    hide a genuine duplicate. Deliberately NOT fuzzy/typo-tolerant
    matching (edit-distance, phonetic matching): that needs a
    similarity threshold tuned against real labeled data, a bigger,
    separate piece of work than this normalization pass."""
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _email_column(table: CanonicalTable) -> str | None:
    return next((c.name for c in table.columns if c.semantic_hint == "email"), None)


def _name_and_company_columns(table: CanonicalTable) -> tuple[list[str], str | None]:
    """Requires BOTH a name-like and a company-like column — name alone
    is deliberately not treated as a business key, since two different
    real people/accounts can legitimately share a name. This mirrors
    the actual review guidance this check was built from: 'same email,
    same normalized name plus company', not name alone.

    Returns the name as a LIST of columns (one `name`-style column, or a
    first+last pair) so callers join them into one composite value."""
    names_by_lower = {c.name.lower(): c.name for c in table.columns}
    company_col = next((names_by_lower[n] for n in _COMPANY_COLUMN_NAMES if n in names_by_lower), None)
    name_col = next((names_by_lower[n] for n in _NAME_COLUMN_NAMES if n in names_by_lower), None)
    if name_col:
        return [name_col], company_col
    first = next((names_by_lower[n] for n in _FIRST_NAME_COLUMN_NAMES if n in names_by_lower), None)
    last = next((names_by_lower[n] for n in _LAST_NAME_COLUMN_NAMES if n in names_by_lower), None)
    if first and last:
        return [first, last], company_col
    return [], company_col


def _phone_column(table: CanonicalTable) -> str | None:
    return next((c.name for c in table.columns if c.semantic_hint == "phone"), None)


def _phone_digits(value: object) -> str | None:
    """Digits only, with a leading US/Canada country code dropped, so
    '+1 (555) 305-6616', '555.305.6616' and '5553056616' all compare
    equal. Fewer than 7 digits is not a usable identity signal."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 7 else None


def _composite_name(row: dict, name_cols: list[str]) -> str | None:
    parts = [row.get(c) for c in name_cols]
    if any(p in (None, "") for p in parts):
        return None
    return _normalize(" ".join(str(p) for p in parts))


@register
class DuplicateCheck(Check):
    """Flags rows that share a real-world identity with another row,
    not just rows that are byte-identical across every column.

    Reproduced bug this fixes: the original version matched on the
    ENTIRE row, including the primary `id` — so two rows describing the
    same person/account with two different IDs (the actual, common
    shape of a CRM duplicate) were completely invisible to it. A probe
    table with 30 rows of one duplicated person under 30 different IDs
    scored uniqueness=100.0, a perfect score, on data that was 30%
    duplicate.

    Detection now runs on the table's real-world identity, in order of
    how strong a signal it is:
    1. A column hinted as `email` (see CSVConnector._infer_semantic_hint)
       — matched after normalization (case/whitespace), since email is
       the single most reliable stable identity signal typically
       present in this kind of data. A shared ROLE mailbox local-part
       (info@, sales@, support@, ...) is excluded from this key
       entirely, same as a blank — a code review flagged these as a
       real false-positive risk, since they legitimately serve multiple
       different real contacts at one company (see _ROLE_LOCAL_PARTS).
    2. A name-like column (or a first_name + last_name pair) PAIRED WITH a
       company-like column (see _name_and_company_columns) — matched on the normalized
       composite. Name alone is deliberately not used as a key. A
       group of 3+ rows sharing one name+company key is reported at
       LOW confidence (CheckResult.row_severity) rather than a certain
       match — common names at large employers make a big group a
       meaningfully weaker signal than a 2-row match (see
       _LARGE_GROUP_LOW_CONFIDENCE_THRESHOLD).
    3. A name paired with a PHONE number (digits only, formatting and a
       leading country code ignored) — the same person recorded again
       with a different email and a differently formatted phone.
    All signals run independently and their results are unioned when
    both are present — every real signal this table offers gets used,
    not just the strongest one. Rows where a key's own value is blank
    are never compared on that key (a shared blank is not a shared
    identity — this was already correct before the edge-case review
    below, verified directly rather than re-implemented).

    Each duplicate group flags every member EXCEPT the earliest-seen
    row (treated as "the original") — flagging that row too would
    double the apparent damage of one underlying duplication event.
    This applies uniformly to both business-key groups and the
    exact-full-row fallback below. An earlier version of this check
    flagged every member of a group, including the original.

    If NEITHER signal is available on this table's schema, this falls
    back to the original exact-full-row match — so a table that
    doesn't match either identity pattern is never LESS covered than
    it was before this fix, even though it also can't yet benefit from
    business-key matching. flagged_fields is deliberately left empty in
    every path (same as before this fix): duplication is a whole-row
    property, not one field's fault, regardless of which signal caught
    it — this check's weight in the scorer stays the flat, unattributed
    default on purpose.

    Still a known, honest limitation: exact-match-after-normalization,
    not fuzzy/typo-tolerant matching (see _normalize's docstring) —
    'Jon Smith' at 'Acme Corp' vs 'Jonathan Smith' at 'Acme Corporation'
    still won't be caught here. That's real future work, not something
    this pass claims to have solved.
    """

    name = "duplicate_check"
    metric = MetricCategory.UNIQUENESS

    def applies_to(self, table: CanonicalTable, context: ScanContext | None = None) -> bool:
        return len(table.rows) > 1

    def _business_keys(self, table: CanonicalTable) -> list[tuple[str, object]]:
        """Returns (label, key_fn) pairs — key_fn(row) -> normalized
        composite key string, or None when this row has nothing
        comparable for that key (a blank component)."""
        keys: list[tuple[str, object]] = []

        email_col = _email_column(table)
        if email_col:
            def email_key(row: dict, col: str = email_col) -> str | None:
                v = row.get(col)
                if v in (None, ""):
                    return None  # a shared blank is not a shared identity
                normalized = _normalize(v)
                local_part = normalized.split("@", 1)[0]
                if local_part in _ROLE_LOCAL_PARTS:
                    return None  # shared role mailbox — not a reliable identity signal
                return normalized

            keys.append(("email", email_key))

        name_cols, company_col = _name_and_company_columns(table)
        if name_cols and company_col:
            def name_company_key(row: dict, nc: list[str] = name_cols, cc: str = company_col) -> str | None:
                nv, cv = _composite_name(row, nc), row.get(cc)
                if nv is None or cv in (None, ""):
                    return None
                return f"{nv}|{_normalize(cv)}"

            keys.append(("name+company", name_company_key))

        # Same person, same phone number — however the number is
        # formatted. Catches the CRM duplicate whose email and phone
        # formatting both differ from the original (a different email
        # address is exactly why the email key misses it). Independent
        # of company, so a changed employer doesn't hide it.
        phone_col = _phone_column(table)
        if name_cols and phone_col:
            def name_phone_key(row: dict, nc: list[str] = name_cols, pc: str = phone_col) -> str | None:
                nv, pv = _composite_name(row, nc), _phone_digits(row.get(pc))
                if nv is None or pv is None:
                    return None
                return f"{nv}|{pv}"

            keys.append(("name+phone", name_phone_key))

        return keys

    def run(self, table: CanonicalTable, context: ScanContext | None = None) -> CheckResult:
        business_keys = self._business_keys(table)
        flagged: set[int] = set()
        row_severity: dict[int, str] = {}
        row_reason_parts: dict[int, list[str]] = defaultdict(list)
        notes: list[str] = []

        if business_keys:
            for label, key_fn in business_keys:
                groups: dict[str, list[int]] = defaultdict(list)
                for i, row in enumerate(table.rows):
                    k = key_fn(row)
                    if k is None:
                        continue  # a shared blank (or role mailbox) is not a shared identity
                    groups[k].append(i)  # appended in row order -> indices[0] is the earliest-seen row

                dup_rows: list[int] = []
                for indices in groups.values():
                    if len(indices) <= 1:
                        continue
                    # Flag all but the earliest-seen row in the group —
                    # that row is treated as "the original"; flagging it
                    # too would double the apparent damage of a single
                    # underlying duplication event (a code review's
                    # explicit correction to the previous "flag every
                    # member" behavior).
                    duplicates_in_group = indices[1:]
                    dup_rows.extend(duplicates_in_group)
                    is_large_name_company_group = (
                        label == "name+company" and len(indices) >= _LARGE_GROUP_LOW_CONFIDENCE_THRESHOLD
                    )
                    if is_large_name_company_group:
                        # Common names at large employers make a big
                        # name+company group a weaker signal than a
                        # 2-row match — still flagged, just not at full
                        # confidence.
                        for i in duplicates_in_group:
                            row_severity[i] = "low"
                    for i in duplicates_in_group:
                        if label == "email":
                            row_reason_parts[i].append("shares the same email address as an earlier row")
                        elif label == "name+phone":
                            row_reason_parts[i].append(
                                "shares the same name and phone number (ignoring formatting) as an earlier row"
                            )
                        elif is_large_name_company_group:
                            row_reason_parts[i].append(
                                "shares the same name and company as an earlier row (low confidence — "
                                "3+ rows share this identity, common at large employers)"
                            )
                        else:
                            row_reason_parts[i].append(
                                "shares the same name and company as an earlier row"
                            )

                if dup_rows:
                    notes.append(
                        f"{label}: {len(dup_rows)} row(s) share a business key with an "
                        f"earlier row in this table"
                    )
                    flagged.update(dup_rows)
        else:
            # Fallback — neither identity signal is available on this
            # schema. Same exact-full-row match as before this fix, so
            # this table is never LESS covered than it used to be.
            groups: dict[tuple, list[int]] = defaultdict(list)
            for i, row in enumerate(table.rows):
                key = tuple(sorted((k, str(v)) for k, v in row.items()))
                groups[key].append(i)
            dup_rows = [i for indices in groups.values() if len(indices) > 1 for i in indices[1:]]
            if dup_rows:
                notes.append(
                    f"exact row match: {len(dup_rows)} row(s) are byte-identical to an "
                    f"earlier row in this table"
                )
                flagged.update(dup_rows)
                for i in dup_rows:
                    row_reason_parts[i].append("is byte-identical to an earlier row in this table")

        # One specific reason per flagged row (which signal(s) actually
        # matched THAT row), rather than reusing the whole check's
        # aggregate `detail` string for every row regardless of which
        # match caused it — see CheckResult.row_reasons' own docstring
        # for the confusing case this fixes.
        row_reasons = {i: "; ".join(parts) for i, parts in row_reason_parts.items()}

        return CheckResult(
            check_name=self.name,
            metric=self.metric,
            flagged_row_indices=sorted(flagged),
            total_rows_evaluated=len(table.rows),
            detail="; ".join(notes) if notes else "no duplicates found",
            row_severity=row_severity,
            row_reasons=row_reasons,
        )

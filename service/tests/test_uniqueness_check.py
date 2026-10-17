from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.uniqueness import DuplicateCheck


def _table(rows: list[dict], columns: list[ColumnSchema] | None = None) -> CanonicalTable:
    cols = columns or [
        ColumnSchema(name="email", type=ColumnType.STRING, nullable=False),
        ColumnSchema(name="amount", type=ColumnType.INTEGER, nullable=False),
    ]
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=cols,
        rows=rows,
    )


def test_does_not_apply_to_single_row_table():
    table = _table([{"email": "a@example.com", "amount": 100}])
    assert DuplicateCheck().applies_to(table) is False


# --- Fallback path: no email hint, no name+company pair -----------------
# (the _table() helper's default schema has no semantic_hint set, so
# these exercise the original exact-full-row behavior unchanged.)

def test_fallback_flags_exact_duplicate_rows():
    """Only row 2 is flagged, not row 0 too — row 0 is "the original"
    (the earliest-seen row in the group); flagging it as well would
    double the apparent damage of one duplication event (a code review's
    explicit correction to the previous "flag every member" behavior)."""
    table = _table(
        [
            {"email": "a@example.com", "amount": 100},
            {"email": "b@example.com", "amount": 50},
            {"email": "a@example.com", "amount": 100},  # exact dup of row 0
        ]
    )
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [2]
    assert result.total_rows_evaluated == 3


def test_fallback_no_false_positive_on_similar_but_different_rows():
    table = _table(
        [
            {"email": "a@example.com", "amount": 100},
            {"email": "a@example.com", "amount": 200},  # same email, different amount
        ]
    )
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == []


# --- Business-key path: email hint --------------------------------------

def _email_hinted_table(rows: list[dict]) -> CanonicalTable:
    return _table(
        rows,
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="amount", type=ColumnType.INTEGER, nullable=True),
        ],
    )


def test_reproduced_bug_catches_business_key_duplicates_with_different_ids():
    """The exact case this fix was written for: 30 rows describing the
    same person under 30 DIFFERENT ids — invisible to the original
    exact-full-row match, caught here via the normalized email. Row 70
    (the earliest of the 30) is "the original" and is not flagged;
    rows 71-99 are."""
    rows = [{"id": i, "email": f"user{i}@x.com", "amount": 10} for i in range(70)]
    rows += [{"id": 1000 + i, "email": "dup@x.com", "amount": 20} for i in range(30)]
    table = _email_hinted_table(rows)

    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == list(range(71, 100))
    assert result.flagged_fields == {}  # still whole-row, unattributed — see docstring


def test_email_matching_is_case_and_whitespace_normalized():
    rows = [
        {"id": 1, "email": "A@Example.com", "amount": 10},
        {"id": 2, "email": " a@example.com ", "amount": 20},
        {"id": 3, "email": "b@example.com", "amount": 30},
    ]
    table = _email_hinted_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [1]  # row 0 is "the original", not flagged


def test_blank_email_never_counts_as_a_shared_key():
    """Called out explicitly in a later code review as (in the
    reviewer's words) "arguably the most important" edge case to fix —
    but verified here as ALREADY correct, not a regression this pass
    needed to repair: email_key returns None for a blank value, so two
    blank-email rows are never grouped as a shared identity in the
    first place."""
    rows = [
        {"id": 1, "email": "", "amount": 10},
        {"id": 2, "email": "", "amount": 20},
        {"id": 3, "email": "b@example.com", "amount": 30},
    ]
    table = _email_hinted_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == []


def test_shared_role_mailbox_is_never_treated_as_a_shared_identity():
    """info@, sales@, support@ and similar role addresses legitimately
    serve multiple different real contacts at one company — grouping
    them as "duplicates" would produce mass false positives on any
    table where several unrelated rows share a generic company inbox."""
    rows = [
        {"id": 1, "email": "info@acme.com", "amount": 10},
        {"id": 2, "email": "INFO@acme.com", "amount": 20},  # same role address, different contact
        {"id": 3, "email": "b@example.com", "amount": 30},
    ]
    table = _email_hinted_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == []


def test_role_mailbox_exclusion_does_not_suppress_a_genuine_email_duplicate():
    rows = [
        {"id": 1, "email": "a@example.com", "amount": 10},
        {"id": 2, "email": "a@example.com", "amount": 20},  # genuine duplicate, not a role address
        {"id": 3, "email": "sales@example.com", "amount": 30},
        {"id": 4, "email": "sales@example.com", "amount": 40},  # role address — not flagged
    ]
    table = _email_hinted_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [1]


# --- Business-key path: name + company ----------------------------------

def _name_company_table(rows: list[dict]) -> CanonicalTable:
    return _table(
        rows,
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="name", type=ColumnType.STRING, nullable=True),
            ColumnSchema(name="company", type=ColumnType.STRING, nullable=True),
        ],
    )


def test_name_alone_is_not_a_business_key_without_company():
    """Two different real people can share a name — requires the
    company pairing before it's treated as an identity match."""
    table = _table(
        [
            {"id": 1, "name": "John Smith", "amount": 10},
            {"id": 2, "name": "John Smith", "amount": 20},  # same name, no company column at all
        ],
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="name", type=ColumnType.STRING, nullable=True),
            ColumnSchema(name="amount", type=ColumnType.INTEGER, nullable=True),
        ],
    )
    # No email hint, no company column -> falls back to exact-full-row,
    # and these two rows differ on "amount", so neither is flagged.
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == []


def test_name_plus_company_composite_catches_duplicates_under_different_ids():
    rows = [
        {"id": 1, "name": "Jane Doe", "company": "Acme Corp"},
        {"id": 2, "name": "jane doe", "company": "  acme corp  "},  # same identity, normalized
        {"id": 3, "name": "Jane Doe", "company": "Globex"},  # same name, different company -> not a dup
    ]
    table = _name_company_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [1]  # row 0 is "the original", not flagged
    assert result.row_severity == {}  # only a 2-row group — full confidence, no tiering


def test_large_name_company_group_is_reported_at_low_confidence():
    """Common names at large employers make a big name+company group a
    meaningfully weaker signal than a 2-row match — still flagged, but
    at low confidence rather than treated as certain."""
    rows = [{"id": i, "name": "John Smith", "company": "Acme Corp"} for i in range(3)]
    table = _name_company_table(rows)
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [1, 2]  # row 0 is "the original"
    assert result.row_severity == {1: "low", 2: "low"}


def test_both_signals_run_when_both_are_present():
    """A table with both an email hint AND a name+company pair should
    catch duplicates matched by EITHER signal, not just the strongest
    one."""
    rows = [
        {"id": 1, "email": "a@x.com", "name": "Jane Doe", "company": "Acme"},
        {"id": 2, "email": "a@x.com", "name": "Someone Else", "company": "Other"},  # email match only
        {"id": 3, "email": "b@x.com", "name": "Jane Doe", "company": "Acme"},  # name+company match only
    ]
    table = _table(
        rows,
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="name", type=ColumnType.STRING, nullable=True),
            ColumnSchema(name="company", type=ColumnType.STRING, nullable=True),
        ],
    )
    result = DuplicateCheck().run(table)
    # Row 0 is "the original" for BOTH its email-key group (with row 1)
    # and its name+company-key group (with row 2) — the earliest row in
    # each group it belongs to, so it's excluded from both, not flagged
    # by virtue of being a duplicate under either signal.
    assert result.flagged_row_indices == [1, 2]


# --- Per-row reasons (row_reasons) ---------------------------------------

def test_row_reasons_are_specific_to_which_signal_actually_matched_that_row():
    """A code review caught main.py reusing the whole check's aggregate
    `detail` string for every flagged row, regardless of which signal
    actually matched THAT row — a row caught only by the email signal
    could show a confusing "name+company: N rows..." note that had
    nothing to do with it. row_reasons fixes this: each flagged row
    gets its own specific reason(s)."""
    rows = [
        {"id": 1, "email": "a@x.com", "name": "Jane Doe", "company": "Acme"},
        {"id": 2, "email": "a@x.com", "name": "Someone Else", "company": "Other"},  # email match only
        {"id": 3, "email": "b@x.com", "name": "Jane Doe", "company": "Acme"},  # name+company match only
    ]
    table = _table(
        rows,
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="name", type=ColumnType.STRING, nullable=True),
            ColumnSchema(name="company", type=ColumnType.STRING, nullable=True),
        ],
    )
    result = DuplicateCheck().run(table)

    # Row 1 was caught ONLY by the email signal — its reason must not
    # mention name+company at all.
    assert result.row_reasons[1] == "shares the same email address as an earlier row"
    # Row 2 was caught ONLY by the name+company signal — its reason
    # must not mention email at all.
    assert result.row_reasons[2] == "shares the same name and company as an earlier row"


def test_row_reasons_combines_both_signals_when_both_match_the_same_row():
    rows = [
        {"id": 1, "email": "a@x.com", "name": "Jane Doe", "company": "Acme"},
        {"id": 2, "email": "a@x.com", "name": "Jane Doe", "company": "Acme"},  # matches both signals
    ]
    table = _table(
        rows,
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER, nullable=False),
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="name", type=ColumnType.STRING, nullable=True),
            ColumnSchema(name="company", type=ColumnType.STRING, nullable=True),
        ],
    )
    result = DuplicateCheck().run(table)
    assert "shares the same email address as an earlier row" in result.row_reasons[1]
    assert "shares the same name and company as an earlier row" in result.row_reasons[1]


def test_row_reasons_for_large_name_company_group_notes_low_confidence():
    rows = [{"id": i, "name": "John Smith", "company": "Acme Corp"} for i in range(3)]
    table = _name_company_table(rows)
    result = DuplicateCheck().run(table)
    assert "low confidence" in result.row_reasons[1]
    assert "low confidence" in result.row_reasons[2]


def test_row_reasons_in_fallback_mode():
    table = _table(
        [
            {"email": "a@example.com", "amount": 100},
            {"email": "b@example.com", "amount": 50},
            {"email": "a@example.com", "amount": 100},  # exact dup of row 0
        ]
    )
    result = DuplicateCheck().run(table)
    assert result.row_reasons[2] == "is byte-identical to an earlier row in this table"


# --- first_name + last_name pairs and the name+phone key ------------------

def _people(rows: list[dict]) -> CanonicalTable:
    cols = [
        ColumnSchema(name="first_name", type=ColumnType.STRING),
        ColumnSchema(name="last_name", type=ColumnType.STRING),
        ColumnSchema(name="company", type=ColumnType.STRING),
        ColumnSchema(name="email", type=ColumnType.STRING, semantic_hint="email"),
        ColumnSchema(name="phone", type=ColumnType.STRING, semantic_hint="phone"),
    ]
    return CanonicalTable(tenant_id="t", source_id="s", table_name="people", columns=cols, rows=rows)


def test_same_person_with_different_email_and_phone_format_is_a_duplicate():
    """The phone_format_duplicate shape from the 300-row test file:
    same name and phone digits, different email, different formatting."""
    table = _people(
        [
            {"first_name": "Barbara", "last_name": "Mensah", "company": "Bright Industries",
             "email": "barbara.mensah@bright.com", "phone": "+1 (555) 305-6616"},
            {"first_name": "Other", "last_name": "Person", "company": "Acme",
             "email": "o@acme.com", "phone": "555-111-2222"},
            {"first_name": "Barbara", "last_name": "Mensah", "company": "Bright Industries",
             "email": "barbara.mensah@personalmail.com", "phone": "5553056616"},
        ]
    )
    result = DuplicateCheck().run(table)
    assert result.flagged_row_indices == [2]  # the original (row 0) is never flagged
    assert "phone" in result.row_reasons[2]


def test_name_phone_key_ignores_a_changed_company():
    table = _people(
        [
            {"first_name": "Ana", "last_name": "Lee", "company": "Old Co",
             "email": "ana@old.com", "phone": "555.222.3333"},
            {"first_name": "Ana", "last_name": "Lee", "company": "New Co",
             "email": "ana@new.com", "phone": "(555) 222-3333"},
        ]
    )
    assert DuplicateCheck().run(table).flagged_row_indices == [1]


def test_same_name_different_phone_and_company_is_not_a_duplicate():
    table = _people(
        [
            {"first_name": "John", "last_name": "Smith", "company": "Acme",
             "email": "j1@a.com", "phone": "555-100-0001"},
            {"first_name": "John", "last_name": "Smith", "company": "Globex",
             "email": "j2@g.com", "phone": "555-100-0002"},
        ]
    )
    assert DuplicateCheck().run(table).flagged_row_indices == []


def test_blank_or_too_short_phone_is_never_a_shared_identity():
    table = _people(
        [
            {"first_name": "Ana", "last_name": "Lee", "company": "A", "email": "a@a.com", "phone": ""},
            {"first_name": "Ana", "last_name": "Lee", "company": "B", "email": "b@b.com", "phone": ""},
            {"first_name": "Bo", "last_name": "Li", "company": "C", "email": "c@c.com", "phone": "123"},
            {"first_name": "Bo", "last_name": "Li", "company": "D", "email": "d@d.com", "phone": "123"},
        ]
    )
    assert DuplicateCheck().run(table).flagged_row_indices == []


def test_first_name_alone_is_not_enough_for_the_name_company_key():
    """Only a first_name column (no last_name) must not be treated as a
    full name — two 'Ana's at one company are not provably the same person."""
    cols = [
        ColumnSchema(name="first_name", type=ColumnType.STRING),
        ColumnSchema(name="company", type=ColumnType.STRING),
    ]
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="p", columns=cols,
        rows=[{"first_name": "Ana", "company": "A"}, {"first_name": "Ana", "company": "A"}],
    )
    # No business key available -> falls back to exact-row match, which DOES
    # flag these two byte-identical rows; the point is the reason is the
    # exact-row one, not a name+company claim.
    result = DuplicateCheck().run(table)
    assert "byte-identical" in result.row_reasons[1]

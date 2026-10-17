from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.validity import FormatValidityCheck


def _table(rows: list[dict]) -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=[
            ColumnSchema(
                name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"
            ),
            ColumnSchema(
                name="phone", type=ColumnType.STRING, nullable=True, semantic_hint="phone"
            ),
            ColumnSchema(
                name="signup_date",
                type=ColumnType.STRING,
                nullable=False,
                semantic_hint="date",
            ),
        ],
        rows=rows,
    )


def test_flags_malformed_email():
    table = _table(
        [{"email": "not-an-email", "phone": "", "signup_date": "2026-01-05"}]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == [0]


def test_does_not_flag_unusual_but_valid_email():
    """Plus-addressing and multi-segment domains are legitimate — this
    is the false-positive guardrail from the design discussion."""
    table = _table(
        [
            {
                "email": "name+work@sub.example.co.uk",
                "phone": "",
                "signup_date": "2026-01-05",
            }
        ]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == []


def test_blank_values_are_not_flagged_here():
    """Blanks are NullCheck's job, not this check's — avoid double-flagging."""
    table = _table([{"email": "", "phone": "", "signup_date": "2026-01-05"}])
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == []


def test_flags_malformed_date():
    table = _table(
        [{"email": "a@example.com", "phone": "", "signup_date": "not-a-date"}]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == [0]


def test_accepts_common_date_formats():
    table = _table(
        [
            {"email": "a@example.com", "phone": "", "signup_date": "01/05/2026"},
            {"email": "b@example.com", "phone": "", "signup_date": "2026-01-05"},
        ]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == []


def test_accepts_reasonable_phone_formats():
    table = _table(
        [
            {"email": "a@example.com", "phone": "+1 (555) 123-4567", "signup_date": "2026-01-05"},
            {"email": "b@example.com", "phone": "555-123-4567", "signup_date": "2026-01-05"},
        ]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == []


def test_field_attribution_identifies_which_field_failed():
    table = _table(
        [
            {"email": "not-an-email", "phone": "555-1234", "signup_date": "not-a-date"},
        ]
    )
    result = FormatValidityCheck().run(table)
    assert result.flagged_row_indices == [0]
    assert set(result.flagged_fields[0]) == {"email", "signup_date"}
    assert "phone" not in result.flagged_fields[0]  # phone was valid, shouldn't be blamed


def test_common_real_world_phone_formats_all_pass_including_parenthesized_area_code():
    """Regression: "(555) 123-4567" was flagged invalid because the
    pattern demanded a leading "+" or digit — a 3,000-row test file with
    ~25% of numbers in that format produced 737 false positives."""
    good = [
        "555-123-4567", "(555) 123-4567", "+1 555 123 4567",
        "+1 (555) 123-4567", "555.123.4567", "5551234567", "+44 20 7946 0958",
    ]
    rows = [
        {"email": f"a{i}@x.com", "phone": p, "signup_date": "2024-01-01"}
        for i, p in enumerate(good)
    ]
    result = FormatValidityCheck().run(_table(rows))
    assert result.flagged_row_indices == []


def test_obviously_bad_phones_are_still_flagged():
    rows = [
        {"email": "a@x.com", "phone": "abc", "signup_date": "2024-01-01"},
        {"email": "b@x.com", "phone": "12", "signup_date": "2024-01-01"},
        {"email": "c@x.com", "phone": "(", "signup_date": "2024-01-01"},
    ]
    result = FormatValidityCheck().run(_table(rows))
    assert result.flagged_row_indices == [0, 1, 2]

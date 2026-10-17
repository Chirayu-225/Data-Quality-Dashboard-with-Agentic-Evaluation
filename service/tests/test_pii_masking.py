from app.agent.pii_masking import columns_to_mask, mask_table_for_llm
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType


def _table(columns: list[ColumnSchema], rows: list[dict]) -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t", source_id="s", table_name="people", columns=columns, rows=rows
    )


def test_email_and_phone_hinted_columns_are_masked():
    table = _table(
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="phone", type=ColumnType.STRING, nullable=False, semantic_hint="phone"),
            ColumnSchema(name="amount", type=ColumnType.FLOAT, nullable=False),
        ],
        rows=[
            {"email": "a@example.com", "phone": "555-1234", "amount": "10"},
            {"email": "b@example.com", "phone": "555-5678", "amount": "20"},
        ],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert masked.rows[0]["email"].startswith("EMAIL_")
    assert masked.rows[0]["phone"].startswith("PHONE_")
    assert "a@example.com" not in str(masked.rows)
    assert "555-1234" not in str(masked.rows)
    # A non-hinted, non-excluded, non-string column is left alone.
    assert masked.rows[0]["amount"] == "10"
    # The original table is never mutated.
    assert table.rows[0]["email"] == "a@example.com"


def test_placeholders_are_keyed_by_value_not_by_row():
    """The core fix: the SAME email in two different rows must map to
    the SAME placeholder, so the semantic layer can still notice two
    records share a contact — a row-indexed placeholder (the old,
    defective design) would destroy that signal."""
    table = _table(
        columns=[ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email")],
        rows=[
            {"email": "shared@example.com"},
            {"email": "different@example.com"},
            {"email": "shared@example.com"},
        ],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert masked.rows[0]["email"] == masked.rows[2]["email"]  # same value -> same token
    assert masked.rows[0]["email"] != masked.rows[1]["email"]  # different value -> different token


def test_placeholder_is_case_insensitive_for_correlation():
    table = _table(
        columns=[ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email")],
        rows=[{"email": "Person@Example.com"}, {"email": "person@example.com"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")
    assert masked.rows[0]["email"] == masked.rows[1]["email"]


def test_different_salts_produce_different_tokens_for_the_same_value():
    """A token's meaning must not carry across two different scans —
    see the module docstring's reversibility claim."""
    table = _table(
        columns=[ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email")],
        rows=[{"email": "a@example.com"}],
    )
    masked_a = mask_table_for_llm(table, salt="scan-one")
    masked_b = mask_table_for_llm(table, salt="scan-two")
    assert masked_a.rows[0]["email"] != masked_b.rows[0]["email"]


def test_blank_values_stay_blank_rather_than_getting_a_fake_placeholder():
    table = _table(
        columns=[ColumnSchema(name="email", type=ColumnType.STRING, nullable=True, semantic_hint="email")],
        rows=[{"email": ""}, {"email": None}, {"email": "a@example.com"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert masked.rows[0]["email"] == ""
    assert masked.rows[1]["email"] is None
    assert masked.rows[2]["email"].startswith("EMAIL_")


def test_caller_specified_exclude_columns_are_masked_with_a_name_derived_placeholder():
    table = _table(
        columns=[
            ColumnSchema(name="first_name", type=ColumnType.STRING, nullable=False),
            ColumnSchema(name="amount", type=ColumnType.FLOAT, nullable=False),
        ],
        rows=[{"first_name": "Jane", "amount": "10"}],
    )
    masked = mask_table_for_llm(table, extra_exclude_columns={"first_name"}, salt="scan-salt")

    assert masked.rows[0]["first_name"].startswith("FIRST_NAME_")
    assert "Jane" not in masked.rows[0]["first_name"]
    assert masked.rows[0]["amount"] == "10"


def test_an_exclude_column_name_that_does_not_exist_on_the_table_is_ignored():
    table = _table(
        columns=[ColumnSchema(name="amount", type=ColumnType.FLOAT, nullable=False)],
        rows=[{"amount": "10"}],
    )
    masked = mask_table_for_llm(table, extra_exclude_columns={"nonexistent_column"}, salt="scan-salt")

    assert masked.rows[0]["amount"] == "10"


def test_a_table_with_nothing_to_mask_or_scrub_is_returned_unchanged_not_copied():
    table = _table(
        columns=[ColumnSchema(name="amount", type=ColumnType.FLOAT, nullable=False)],
        rows=[{"amount": "10"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert masked is table  # no pointless copy when nothing needs masking or scrubbing


def test_columns_to_mask_reports_the_hint_for_auto_masked_columns_and_none_for_excluded_ones():
    table = _table(
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="first_name", type=ColumnType.STRING, nullable=False),
        ],
        rows=[],
    )
    result = columns_to_mask(table, extra_exclude_columns={"first_name"})
    assert result == {"email": "email", "first_name": None}


def test_date_hinted_columns_are_structurally_untouched_since_a_date_is_not_pii():
    table = _table(
        columns=[ColumnSchema(name="signup_date", type=ColumnType.STRING, nullable=False, semantic_hint="date")],
        rows=[{"signup_date": "2024-01-01"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert masked.rows[0]["signup_date"] == "2024-01-01"


# --- Embedded (regex) PII scrub in unhinted free-text columns -------------


def test_email_embedded_in_a_free_text_notes_column_is_scrubbed():
    table = _table(
        columns=[ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True)],
        rows=[{"notes": "please follow up with jane@example.com about the renewal"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    notes = masked.rows[0]["notes"]
    assert "jane@example.com" not in notes
    assert "EMAIL_" in notes
    assert "please follow up with" in notes  # surrounding text preserved
    assert "about the renewal" in notes


def test_phone_embedded_in_free_text_is_scrubbed_but_bare_numeric_ids_are_not():
    table = _table(
        columns=[ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True)],
        rows=[{"notes": "call 555-867-5309, re order 1234567890"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    notes = masked.rows[0]["notes"]
    assert "555-867-5309" not in notes
    assert "PHONE_" in notes
    assert "1234567890" in notes  # a bare digit run (no separators) is not phone-shaped enough


def test_an_unhinted_column_named_mobile_still_gets_embedded_scrubbing():
    """The structural layer only fires for a column named exactly
    `phone`/`email`-ish per the connector's naming heuristic — `mobile`
    isn't recognized by that hint, so this is exactly the gap the
    embedded scrub exists to close."""
    table = _table(
        columns=[ColumnSchema(name="mobile", type=ColumnType.STRING, nullable=True)],
        rows=[{"mobile": "+1 (555) 234-5678"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    assert "555) 234-5678" not in masked.rows[0]["mobile"]
    assert "PHONE_" in masked.rows[0]["mobile"]


def test_embedded_scrub_correlates_with_structural_masking_of_the_same_value():
    """The SAME email, once in a structurally-masked `email` column and
    once typed into a free-text `notes` field, must produce the SAME
    token in both places — otherwise the correlation signal masking
    exists to preserve breaks the moment PII leaks into unhinted text."""
    table = _table(
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
            ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True),
        ],
        rows=[{"email": "a@example.com", "notes": "reach out to a@example.com directly"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")

    masked_email = masked.rows[0]["email"]
    assert masked_email in masked.rows[0]["notes"]


def test_a_column_with_no_pii_shaped_substrings_is_left_untouched():
    table = _table(
        columns=[ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True)],
        rows=[{"notes": "shipped on time, no issues"}],
    )
    masked = mask_table_for_llm(table, salt="scan-salt")
    assert masked.rows[0]["notes"] == "shipped on time, no issues"


# --- Phone normalization + near-miss corpus --------------------------------

import pytest


def _scrub(text: str) -> str:
    table = _table(
        columns=[ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True)],
        rows=[{"notes": text}],
    )
    return mask_table_for_llm(table, salt="scan-salt").rows[0]["notes"]


def test_differently_formatted_phone_numbers_share_one_token():
    """Formatting varies most in phone fields; duplicate correlation
    must survive it."""
    variants = [
        "+1 (555) 123-4567",
        "555-123-4567",
        "(555) 123-4567",
        "1.555.123.4567",
        "5551234567",
        "+15551234567",
        "555-123-4567 ext. 89",
    ]
    table = _table(
        columns=[ColumnSchema(name="phone", type=ColumnType.STRING, nullable=False, semantic_hint="phone")],
        rows=[{"phone": v} for v in variants] + [{"phone": "555-999-0000"}],
    )
    masked = [r["phone"] for r in mask_table_for_llm(table, salt="scan-salt").rows]
    assert len(set(masked[:-1])) == 1
    assert masked[-1] != masked[0]


def test_a_country_code_or_trunk_zero_does_not_split_one_number_into_two_tokens():
    table = _table(
        columns=[ColumnSchema(name="phone", type=ColumnType.STRING, nullable=False, semantic_hint="phone")],
        rows=[{"phone": "+44 20 7946 0958"}, {"phone": "020 7946 0958"}],
    )
    masked = mask_table_for_llm(table, salt="s").rows
    assert masked[0]["phone"] == masked[1]["phone"]


def test_embedded_and_structural_phone_tokens_match_across_formats():
    table = _table(
        columns=[
            ColumnSchema(name="phone", type=ColumnType.STRING, nullable=False, semantic_hint="phone"),
            ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True),
        ],
        rows=[{"phone": "5551234567", "notes": "also reachable at (555) 123-4567"}],
    )
    row = mask_table_for_llm(table, salt="s").rows[0]
    assert row["phone"] in row["notes"]


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-15",
        "called on 2024-01-15",
        "called on 2024-01-15 and again 2024-02-01",
        "15/01/2024",
        "01.02.2024",
        "2024/01/15",
        "2024-01-15 10:30:00",
        "192.168.1.100",
        "server 10.0.0.1 down",
        "version 2.10.1234567",
        "build 10.0.19045.3693",
        "order 1234-5678-90",
        "ORD-2024-001234",
        "invoice #20240115-0042",
        "SKU 12345678",
        "ref 1234567890",
        "$1,234,567.89",
        "shipped 3 boxes, 12 pallets",
        "ratio 1.25 to 2.50",
    ],
)
def test_near_miss_strings_are_never_mistaken_for_phone_numbers(text):
    assert _scrub(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "call 555-123-4567 today",
        "call (555) 123-4567 today",
        "call +1 (555) 123-4567 today",
        "call 555.123.4567 today",
        "call +44 20 7946 0958 today",
        "call +919876543210 today",
        "call 98765 43210 today",
        "call 555 1234 today",
        "call 01 23 45 67 89 today",
    ],
)
def test_real_phone_shapes_are_still_masked(text):
    out = _scrub(text)
    assert "PHONE_" in out
    assert out.startswith("call ") and out.endswith(" today")


def test_a_phone_next_to_a_date_is_masked_without_eating_the_date():
    out = _scrub("2024-01-15 call 555-123-4567")
    assert out.startswith("2024-01-15 call ")
    assert "PHONE_" in out
    assert "555-123-4567" not in out


def test_dates_in_unhinted_free_text_columns_survive():
    table = _table(
        columns=[ColumnSchema(name="comments", type=ColumnType.STRING, nullable=True)],
        rows=[{"comments": "renewal due 2024-03-31, last contact 12/05/2024"}],
    )
    out = mask_table_for_llm(table, salt="s").rows[0]["comments"]
    assert out == "renewal due 2024-03-31, last contact 12/05/2024"


def test_every_prompt_that_sees_masked_rows_explains_the_masking_tokens():
    """Live finding: without this note the model flagged 37 ordinary rows as
    'email placeholder' (it read EMAIL_<hex> tokens as fake emails) and the
    verifier confirmed them."""
    from app.agent import cross_table_reasoning as cross
    from app.agent import semantic_reasoning as sem
    from app.agent import verifier as ver
    from app.agent.pii_masking import MASKING_NOTE

    for prompt in (
        sem._SYSTEM_PROMPT,
        ver._VERIFIER_SYSTEM_PROMPT,
        cross._SYSTEM_PROMPT,
        cross._VERIFIER_SYSTEM_PROMPT,
    ):
        assert prompt.endswith(MASKING_NOTE)


def test_masking_note_describes_the_real_token_shape():
    import re

    from app.agent.pii_masking import MASKING_NOTE, _token

    token = _token("EMAIL", "someone@example.com", "salt")
    assert re.fullmatch(r"EMAIL_[0-9a-f]{10}", token)
    assert "EMAIL_" in MASKING_NOTE and "PHONE_" in MASKING_NOTE
    assert "NOT a defect" in MASKING_NOTE


def test_valid_value_rule_reaches_the_two_prompts_that_judge_single_table_flags():
    """The 'NA is Namibia' false flags came from the generator and were
    waved through by the verifier, so both must carry the rule."""
    import app.agent.semantic_reasoning as sem
    import app.agent.verifier as ver
    from app.agent.pii_masking import MASKING_NOTE
    from app.agent.prompt_rules import VALID_VALUE_RULE

    for prompt in (sem._SYSTEM_PROMPT, ver._VERIFIER_SYSTEM_PROMPT):
        assert VALID_VALUE_RULE in prompt
        assert prompt.endswith(MASKING_NOTE)  # MASKING_NOTE must stay last


def test_valid_value_rule_is_a_general_principle_not_a_list_of_test_values():
    from app.agent.prompt_rules import VALID_VALUE_RULE

    assert "ISO 3166-1" in VALID_VALUE_RULE and "Namibia" in VALID_VALUE_RULE
    # must not name the planted misses it should not be tuned on
    for planted in ("test test test", "asdfgh", "qwerty", "Wire Transfer", "Accounts Payable"):
        assert planted not in VALID_VALUE_RULE

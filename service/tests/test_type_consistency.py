from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.type_consistency import TypeConsistencyCheck


def _table(column_type: ColumnType, rows: list[dict], column_name: str = "amount") -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="t1",
        columns=[ColumnSchema(name=column_name, type=column_type, nullable=False)],
        rows=rows,
    )


def test_flags_a_value_that_does_not_match_an_integer_column():
    """The core acceptance test: a column inferred INTEGER (95% clean)
    with a genuinely bad value ("abc") flags just that row under
    validity."""
    table = _table(
        ColumnType.INTEGER,
        [{"amount": "100"}, {"amount": "abc"}, {"amount": "150"}],
    )
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == [1]
    assert result.flagged_fields[1] == ["amount"]


def test_does_not_flag_blank_values_completeness_is_not_this_checks_job():
    table = _table(
        ColumnType.INTEGER,
        [{"amount": "100"}, {"amount": ""}, {"amount": None}],
    )
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == []


def test_a_95_5_mixed_column_flags_only_the_5_percent():
    rows = [{"amount": str(100 + i)} for i in range(95)] + [{"amount": "abc"} for _ in range(5)]
    table = _table(ColumnType.INTEGER, rows)
    result = TypeConsistencyCheck().run(table)
    assert len(result.flagged_row_indices) == 5
    assert set(result.flagged_row_indices) == set(range(95, 100))


def test_zip_code_column_with_leading_zeros_produces_no_flags():
    """By the time a column reaches this check as STRING (because
    csv_connector.py's own inference correctly declined to call a
    leading-zero column INTEGER), there is nothing for
    TypeConsistencyCheck to flag at all -- it only checks
    INTEGER/FLOAT/BOOLEAN-typed columns."""
    table = _table(
        ColumnType.STRING,
        [{"amount": "02134"}, {"amount": "01801"}, {"amount": "00501"}],
    )
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == []


def test_flags_a_value_that_does_not_match_a_float_column():
    table = _table(
        ColumnType.FLOAT,
        [{"amount": "100.50"}, {"amount": "not-a-number"}, {"amount": "$1,200.00"}],
    )
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == [1]  # currency parses fine, "not-a-number" doesn't


def test_flags_a_value_that_does_not_match_a_boolean_column():
    table = _table(
        ColumnType.BOOLEAN,
        [{"amount": "yes"}, {"amount": "maybe"}, {"amount": "no"}],
    )
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == [1]


def test_mixed_string_column_gets_one_column_level_note_not_per_row_flags():
    """A genuinely mixed/freeform column (below the 90% majority
    threshold that would have made it INTEGER/FLOAT) gets a single
    informational note in `detail`, with zero rows flagged -- flagging
    dozens of rows individually in a column that was never going to be
    "fixed" into one type isn't useful."""
    rows = [{"amount": str(100 + i)} for i in range(20)] + [{"amount": f"note-{i}"} for i in range(80)]
    table = _table(ColumnType.STRING, rows)
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == []
    assert "mixed-type column" in result.detail
    assert "20%" in result.detail


def test_clean_string_column_with_no_numeric_content_gets_no_note():
    table = _table(ColumnType.STRING, [{"amount": "Acme Corp"}, {"amount": "Globex"}])
    result = TypeConsistencyCheck().run(table)
    assert result.flagged_row_indices == []
    assert "mixed-type column" not in result.detail


def test_applies_to_is_false_for_a_table_with_no_typed_or_string_columns():
    table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="t1",
        columns=[ColumnSchema(name="d", type=ColumnType.DATE, nullable=False)],
        rows=[{"d": "2026-01-01"}],
    )
    assert TypeConsistencyCheck().applies_to(table) is False

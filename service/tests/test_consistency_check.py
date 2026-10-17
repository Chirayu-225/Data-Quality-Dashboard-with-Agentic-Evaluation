from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.consistency import OutlierCheck


def _table(amounts: list[float | None]) -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="deals",
        columns=[ColumnSchema(name="amount", type=ColumnType.FLOAT, nullable=True)],
        rows=[{"amount": a} for a in amounts],
    )


def test_flags_extreme_outlier():
    table = _table([100, 110, 90, 105, 95, 100000])
    result = OutlierCheck().run(table)
    assert 5 in result.flagged_row_indices


def test_does_not_flag_tightly_clustered_values():
    table = _table([100, 102, 98, 101, 99, 103])
    result = OutlierCheck().run(table)
    assert result.flagged_row_indices == []


def test_skips_check_below_minimum_sample_size():
    """Avoid meaningless stats on tiny samples — this is the
    false-positive guardrail, not a bug."""
    table = _table([100, 100000])  # only 2 values, well below threshold
    assert OutlierCheck().applies_to(table) is True  # column is numeric...
    result = OutlierCheck().run(table)
    assert result.flagged_row_indices == []  # ...but too few values to judge

def test_recognizes_currency_formatted_values_as_numeric():
    """Shares app/canonical/type_conformity.py's parser with type
    inference and TypeConsistencyCheck -- a bare float(v) would have
    silently skipped "$100,000" instead of treating it as the outlier
    it is."""
    table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="deals",
        columns=[ColumnSchema(name="amount", type=ColumnType.FLOAT)],
        rows=[{"amount": v} for v in ["$100", "$110", "$90", "$105", "$95", "$100,000"]],
    )
    result = OutlierCheck().run(table)
    assert 5 in result.flagged_row_indices


def test_does_not_apply_to_non_numeric_table():
    table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=[ColumnSchema(name="email", type=ColumnType.STRING)],
        rows=[{"email": "a@example.com"}],
    )
    assert OutlierCheck().applies_to(table) is False


def test_field_attribution_identifies_which_column_was_the_outlier():
    table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="deals",
        columns=[
            ColumnSchema(name="amount", type=ColumnType.FLOAT),
            ColumnSchema(name="quantity", type=ColumnType.FLOAT),
        ],
        rows=[
            {"amount": 100, "quantity": 5},
            {"amount": 110, "quantity": 6},
            {"amount": 90, "quantity": 4},
            {"amount": 105, "quantity": 5},
            {"amount": 95, "quantity": 5},
            {"amount": 100000, "quantity": 5},  # only amount is the outlier here
        ],
    )
    result = OutlierCheck().run(table)
    assert 5 in result.flagged_row_indices
    assert result.flagged_fields[5] == ["amount"]  # not quantity — quantity was normal

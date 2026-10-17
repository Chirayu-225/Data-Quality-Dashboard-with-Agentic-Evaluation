import csv
from pathlib import Path

from app.canonical.models import ColumnType
from app.connectors.base import ConnectorConfig
from app.connectors.csv_connector import CSVConnector


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "leads.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "amount"])
        writer.writerow(["a@example.com", "100"])
        writer.writerow(["", "-50"])  # missing email -> should flag as nullable
        writer.writerow(["b@example.com", "75"])
    return path


def test_csv_connector_extracts_canonical_table(tmp_path):
    path = _write_fixture(tmp_path)
    config = ConnectorConfig(
        tenant_id="tenant-1", source_id="csv-1", settings={"file_path": str(path)}
    )
    connector = CSVConnector(config)

    assert connector.test_connection() is True

    tables = connector.extract()
    assert len(tables) == 1
    table = tables[0]
    assert table.tenant_id == "tenant-1"
    assert table.column_names() == {"email", "amount"}
    assert len(table.rows) == 3


def test_csv_connector_test_connection_fails_on_missing_file():
    config = ConnectorConfig(
        tenant_id="t", source_id="s", settings={"file_path": "/no/such/file.csv"}
    )
    connector = CSVConnector(config)
    assert connector.test_connection() is False


def test_nullability_inferred_from_blank_ratio(tmp_path):
    """A column blank in a small minority of rows should be treated as
    required (nullable=False), not excused from the completeness check
    just because it has *any* blank. A column blank in most rows should
    be treated as genuinely optional."""
    path = tmp_path / "mixed.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "middle_name"])
        # email: blank in 1 of 4 rows (25%) -> should read as required
        writer.writerow(["a@example.com", ""])
        writer.writerow(["b@example.com", "Lee"])
        writer.writerow(["", ""])
        writer.writerow(["c@example.com", ""])
        # middle_name: blank in 3 of 4 rows (75%) -> should read as optional

    config = ConnectorConfig(
        tenant_id="t", source_id="s", settings={"file_path": str(path)}
    )
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}

    assert columns_by_name["email"].nullable is False
    assert columns_by_name["middle_name"].nullable is True


# --- Type-drift fix (formerly a reproduced, NOT fixed, gap — "probe 5") ---
#
# _infer_type() used to sample only the first 50 non-blank values of a
# column, requiring ALL of them to parse. A column that read as clean
# INTEGER for its first 50 rows could drift to a non-numeric value past
# that sample point with nothing downstream catching it. Fixed by: (1)
# looking at the WHOLE column, so position no longer matters, and (2) a
# 90% majority threshold, so a column doesn't flip to STRING just
# because a handful of genuinely bad values exist — those are left for
# TypeConsistencyCheck (tests/test_type_consistency.py) to flag under
# validity instead. "N/A" specifically is now normalized to blank at
# ingest (a null sentinel, not a type violation — see
# test_null_sentinels_normalize_to_blank_at_ingest below), so it no
# longer even reaches type inference as a value to drift into.


def _drifting_integer_column_csv(path: Path, drift_value: str = "abc") -> Path:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "amount"])
        for i in range(50):
            writer.writerow([i, str(100 + i)])  # 50 clean integers
        writer.writerow([50, drift_value])  # drifts to non-numeric past the old 50-sample window
    return path


def test_type_inference_looks_at_the_whole_column_not_just_a_sample(tmp_path):
    """A single genuinely-bad value past where the old 50-value sample
    window used to end no longer silently vanishes from consideration:
    the column is still correctly INTEGER (51 of 52 values conform,
    well above the 90% threshold), and the drifted value survives in
    the row data uncaught by type inference itself — TypeConsistencyCheck,
    not the connector, is what's responsible for flagging it."""
    path = _drifting_integer_column_csv(tmp_path / "drift.csv")
    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]

    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["amount"].type == ColumnType.INTEGER

    drifted_row = table.rows[50]
    assert drifted_row["amount"] == "abc"


def test_type_inference_falls_back_to_string_below_the_majority_threshold(tmp_path):
    """A column that's NOT overwhelmingly one type (here, roughly half
    numeric, half not) is left as STRING rather than forced into
    INTEGER/FLOAT -- the majority threshold, not just "did at least one
    value parse"."""
    path = tmp_path / "mixed.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "amount"])
        for i in range(25):
            writer.writerow([i, str(100 + i)])
        for i in range(25, 50):
            writer.writerow([i, "unknown"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["amount"].type == ColumnType.STRING


def test_type_inference_tolerates_a_small_minority_of_bad_values(tmp_path):
    """The actual majority-threshold behavior: 95 clean integers + 5
    genuinely bad values is still INTEGER overall -- the 5 bad values
    are TypeConsistencyCheck's job to flag individually, not type
    inference's job to react to by abandoning the type altogether."""
    path = tmp_path / "mostly_clean.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "amount"])
        for i in range(95):
            writer.writerow([i, str(100 + i)])
        for i in range(95, 100):
            writer.writerow([i, "abc"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["amount"].type == ColumnType.INTEGER


def test_null_sentinels_normalize_to_blank_at_ingest(tmp_path):
    """"N/A", "null", "-", etc. mean missing data, not a value of the
    wrong type -- they're normalized to blank cells at ingest, so a
    column that's otherwise clean integers isn't knocked down to STRING
    (or, pre-fix, silently accepted as drift) just because some cells
    use a sentinel instead of being truly empty. The row's own data
    shows the normalized blank, not the original sentinel text."""
    path = tmp_path / "sentinels.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "amount"])
        writer.writerow([0, "100"])
        writer.writerow([1, "N/A"])
        writer.writerow([2, "null"])
        writer.writerow([3, "-"])
        writer.writerow([4, "150"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}

    # Sentinels normalized to blank, not counted as non-numeric values --
    # the column reads as INTEGER from its 2 genuinely-numeric values,
    # with the 3 sentinel cells now blank rather than "N/A"/"null"/"-".
    assert columns_by_name["amount"].type == ColumnType.INTEGER
    assert table.rows[1]["amount"] == ""
    assert table.rows[2]["amount"] == ""
    assert table.rows[3]["amount"] == ""


def test_null_sentinels_list_is_configurable_per_connector(tmp_path):
    """A caller can override the default sentinel list via
    settings["null_sentinels"] -- e.g. a connector that should NOT treat
    "-" as a sentinel for a dataset where "-" is a legitimate value. Uses
    a numeric-leaning column so sentinel eligibility (see
    test_sentinel_normalization_is_gated_to_numeric_or_date_leaning_columns)
    doesn't mask whether the custom list itself is honored."""
    path = tmp_path / "custom_sentinels.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["amount"])
        writer.writerow(["100"])
        writer.writerow(["-"])
        writer.writerow(["MISSING"])
        writer.writerow(["150"])

    config = ConnectorConfig(
        tenant_id="t",
        source_id="s",
        settings={"file_path": str(path), "null_sentinels": ["missing"]},
    )
    table = CSVConnector(config).extract()[0]
    # "-" is NOT in the custom list, so it's left alone as a literal
    # value; "MISSING" IS in the custom list, so it's normalized.
    assert table.rows[1]["amount"] == "-"
    assert table.rows[2]["amount"] == ""


def test_sentinel_normalization_is_gated_to_numeric_or_date_leaning_columns(tmp_path):
    """The core fix: a sentinel string is only ever "missing" in a
    column that otherwise leans numeric or date. "NA" is Namibia's own
    country code, and "None"/"-" are legitimate category values in
    plenty of real schemas -- normalizing them globally manufactured a
    completeness failure out of a clean categorical column. A column
    with no numeric/date lean at all is left completely untouched."""
    path = tmp_path / "categories.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["country_code", "allergy"])
        writer.writerow(["NA", "None"])
        writer.writerow(["US", "Peanuts"])
        writer.writerow(["UK", "None"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]

    assert table.rows[0]["country_code"] == "NA"
    assert table.rows[0]["allergy"] == "None"
    assert table.rows[2]["allergy"] == "None"
    # Nothing was normalized, so there's nothing to preserve for display.
    assert table.raw_overrides == {}


def test_sentinel_raw_value_is_preserved_for_display_when_normalized(tmp_path):
    """A cell that DOES get normalized (eligible column) keeps its
    original sentinel text available via raw_overrides, so a caller
    building a human-facing view (main.py's flagged_rows/
    security_findings row_data) can show "N/A" instead of an
    unexplained blank cell -- while `rows` itself still holds the blank
    every check and the scorer need to see."""
    path = tmp_path / "amounts.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["amount"])
        writer.writerow(["100"])
        writer.writerow(["N/A"])
        writer.writerow(["150"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]

    assert table.rows[1]["amount"] == ""
    assert table.raw_overrides == {1: {"amount": "N/A"}}
    assert table.display_row(1)["amount"] == "N/A"
    assert table.display_row(0)["amount"] == "100"  # untouched row needs no override


def test_sentinel_eligibility_also_recognizes_date_leaning_columns(tmp_path):
    """A column of dates with a sprinkled sentinel is just as eligible
    for normalization as a numeric column -- "missing" doesn't only
    happen in numeric fields."""
    path = tmp_path / "dates.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["signup_date"])
        writer.writerow(["2024-01-15"])
        writer.writerow(["N/A"])
        writer.writerow(["2024-03-02"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]

    assert table.rows[1]["signup_date"] == ""
    assert table.raw_overrides == {1: {"signup_date": "N/A"}}


def test_a_column_that_is_entirely_sentinel_values_is_left_ineligible(tmp_path):
    """With no real (non-sentinel) values in the column at all, there's
    nothing to judge "leans numeric/date" from -- the safer default is
    to leave the column alone rather than guess."""
    path = tmp_path / "all_sentinel.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["field"])
        writer.writerow(["N/A"])
        writer.writerow(["null"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]

    assert table.rows[0]["field"] == "N/A"
    assert table.rows[1]["field"] == "null"
    assert table.raw_overrides == {}


def test_type_inference_keeps_leading_zero_values_as_string(tmp_path):
    """Zip-code trap: a column of leading-zero numeric-looking strings
    must not be classified INTEGER, since int("02134") == 2134 loses the
    leading zero -- it isn't really the same value read back."""
    path = tmp_path / "zips.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["zip"])
        for z in ["02134", "01801", "00501", "02139", "01002"]:
            writer.writerow([z])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["zip"].type == ColumnType.STRING
    assert table.rows[0]["zip"] == "02134"  # leading zero preserved, not silently dropped


def test_type_inference_recognizes_currency_and_boolean_variants(tmp_path):
    """Currency-formatted numbers infer as FLOAT, and yes/no reads as
    BOOLEAN alongside true/false -- both real-world formats, not just
    the narrowest literal int()/float() parse."""
    path = tmp_path / "formats.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["price", "active"])
        writer.writerow(["$1,200.00", "yes"])
        writer.writerow(["$1,350.50", "no"])
        writer.writerow(["$999.99", "yes"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["price"].type == ColumnType.FLOAT
    assert columns_by_name["active"].type == ColumnType.BOOLEAN


def test_type_inference_does_not_reclassify_plain_zero_one_as_boolean(tmp_path):
    """A column of plain "1"/"0" stays INTEGER -- see
    type_conformity.py's module docstring for why 1/0 is deliberately
    excluded from boolean recognition (it's ambiguous with an ordinary
    integer flag column)."""
    path = tmp_path / "flags.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["is_active"])
        writer.writerow(["1"])
        writer.writerow(["0"])
        writer.writerow(["1"])

    config = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    table = CSVConnector(config).extract()[0]
    columns_by_name = {c.name: c for c in table.columns}
    assert columns_by_name["is_active"].type == ColumnType.INTEGER

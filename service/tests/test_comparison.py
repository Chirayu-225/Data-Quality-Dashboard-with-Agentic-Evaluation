"""
Tests for the profiler and the two-version comparison.

Two kinds: unit tests of the building blocks, and truth-file tests that
run the comparison on the three forged versions in
eval/datasets/versions/ and assert it reproduces versions_truth.json
(ids, modified fields, PSI bands, rename, schema change). PSI is
compared by BAND and within a tolerance, not exact digits, because the
reference file excludes known outliers and uses its own binning.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.comparison import ComparisonError, compare_tables, profile_table
from app.comparison.compare import (
    categorical_psi,
    normalize_cell,
    numeric_psi,
    psi_band,
)
from app.connectors.base import ConnectorConfig
from app.connectors.csv_connector import CSVConnector

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "eval" / "datasets" / "versions"
TRUTH = json.loads((VERSIONS_DIR / "versions_truth.json").read_text(encoding="utf-8"))


def _load(name: str) -> CanonicalTable:
    cfg = ConnectorConfig(
        tenant_id="t", source_id="s", settings={"file_path": str(VERSIONS_DIR / f"leads_{name}.csv")}
    )
    return CSVConnector(cfg).extract()[0]


def _table(rows: list[dict], name: str = "t", types: dict | None = None) -> CanonicalTable:
    cols = list(rows[0].keys()) if rows else []
    types = types or {}
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name=name,
        columns=[ColumnSchema(name=c, type=types.get(c, ColumnType.STRING)) for c in cols],
        rows=rows,
    )


@pytest.fixture(scope="module")
def v1() -> CanonicalTable:
    return _load("v1")


@pytest.fixture(scope="module")
def v2() -> CanonicalTable:
    return _load("v2")


@pytest.fixture(scope="module")
def v3() -> CanonicalTable:
    return _load("v3")


# ----------------------------------------------------------- normalization


def test_currency_and_plain_numbers_are_equal():
    assert normalize_cell("$12,345.00") == normalize_cell("12345")
    assert normalize_cell(" 12345 ") == normalize_cell("12345.0")


def test_leading_zero_zip_is_not_collapsed_into_a_number():
    assert normalize_cell("02134") != normalize_cell("2134")


def test_nan_and_inf_strings_are_text_not_numbers():
    assert normalize_cell("nan").startswith("s:")
    assert normalize_cell("Infinity").startswith("s:")


def test_huge_integers_do_not_collapse_by_float_rounding():
    assert normalize_cell("12345678901234567890") != normalize_cell("12345678901234567891")


def test_blank_and_none_are_the_same():
    assert normalize_cell("") == normalize_cell(None) == ""


# --------------------------------------------------------------------- PSI


def test_identical_distributions_have_zero_psi():
    vals = ["a"] * 50 + ["b"] * 50
    assert categorical_psi(vals, vals) == pytest.approx(0.0, abs=1e-9)
    nums = [float(i) for i in range(100)]
    assert numeric_psi(nums, nums) == pytest.approx(0.0, abs=1e-9)


def test_shifted_distribution_has_positive_psi():
    old = [float(i) for i in range(200)]
    new = [float(i) + 150 for i in range(200)]
    assert numeric_psi(old, new) > 0.2


def test_psi_with_heavy_ties_does_not_crash():
    assert numeric_psi([1.0] * 100, [1.0] * 100) == pytest.approx(0.0, abs=1e-9)


def test_psi_bands():
    assert psi_band(0.05, 0.10, 0.20) == "stable"
    assert psi_band(0.10, 0.10, 0.20) == "warning"
    assert psi_band(0.20, 0.10, 0.20) == "critical"


def test_thresholds_are_configurable(v1, v2):
    strict = compare_tables(v1, v2, psi_warn=0.001, psi_critical=0.01)
    bands = {d.column: d.band for d in strict.drift}
    assert bands["discount_type"] == "warning"  # 0.0021 sits between 0.001 and 0.01


def test_invalid_threshold_order_is_rejected(v1, v2):
    with pytest.raises(ComparisonError):
        compare_tables(v1, v2, psi_warn=0.3, psi_critical=0.2)


# --------------------------------------------------- truth: v1 -> v2


def test_v1_v2_row_summary_matches_truth(v1, v2):
    r = compare_tables(v1, v2, max_detail_rows=10_000)
    t = TRUTH["v1_to_v2"]
    assert r.key_column == "id"
    assert r.rows.matching == "by_key"
    assert (r.rows.unchanged, r.rows.modified, r.rows.added, r.rows.removed) == (
        t["unchanged_count"], t["modified"]["count"], len(t["added_ids"]), len(t["removed_ids"]),
    )
    assert r.summary.startswith("890 unchanged, 60 modified, 250 added, 50 removed")


def test_v1_v2_ids_and_changed_fields_match_truth(v1, v2):
    r = compare_tables(v1, v2, max_detail_rows=10_000)
    t = TRUTH["v1_to_v2"]
    assert sorted(int(k) for k in r.rows.removed_keys) == t["removed_ids"]
    assert sorted(int(k) for k in r.rows.added_keys) == t["added_ids"]
    got = {int(m.key): [c.column for c in m.changed_fields] for m in r.rows.modified_rows}
    assert got == {int(k): v for k, v in t["modified"]["changed_fields_by_id"].items()}


def test_v1_v2_drift_bands_match_truth(v1, v2):
    r = compare_tables(v1, v2)
    drift = {d.column: d for d in r.drift}
    ref = TRUTH["v1_to_v2"]["reference_metrics"]
    assert drift["country_code"].band == "critical"
    assert drift["country_code"].value == pytest.approx(ref["psi_country_code"], abs=0.005)
    assert drift["amount"].band == "warning"
    assert drift["amount"].value == pytest.approx(ref["psi_amount"], abs=0.03)
    assert drift["discount_type"].band == "stable"
    assert drift["discount_type"].value == pytest.approx(ref["psi_discount_type_control"], abs=0.005)


def test_v1_v2_volume_and_null_rate_match_truth(v1, v2):
    r = compare_tables(v1, v2)
    ref = TRUTH["v1_to_v2"]["reference_metrics"]
    assert r.volume_change_pct == ref["volume_change_pct"]
    assert r.volume_flagged
    phone = next(n for n in r.null_rate_changes if n.column == "phone")
    assert phone.old_rate == ref["phone_null_rate"]["v1"]
    assert phone.new_rate == pytest.approx(ref["phone_null_rate"]["v2"], abs=0.001)
    assert phone.flagged
    others = [n.column for n in r.null_rate_changes if n.flagged and n.column != "phone"]
    assert others == []


def test_comparison_is_independent_of_row_order(v1, v2):
    shuffled = v2.model_copy(update={"rows": list(reversed(v2.rows))})
    a = compare_tables(v1, v2)
    b = compare_tables(v1, shuffled)
    assert a.hashes.new_content_hash == b.hashes.new_content_hash
    assert (a.rows.unchanged, a.rows.modified, a.rows.added, a.rows.removed) == (
        b.rows.unchanged, b.rows.modified, b.rows.added, b.rows.removed,
    )


# --------------------------------------------------- truth: v2 -> v3


def test_v2_v3_schema_changes_match_truth(v2, v3):
    r = compare_tables(v2, v3, max_detail_rows=10_000)
    t = TRUTH["v2_to_v3"]["schema_changes"]
    s = r.schema_diff
    assert [(x.old_name, x.new_name) for x in s.renamed] == list(t["renamed"].items())
    assert s.renamed[0].agreement >= 0.99
    assert s.added_columns == t["added_columns"]
    assert s.removed_columns == t["removed_columns"]
    assert s.column_order_changed


def test_v2_v3_reformatted_amount_is_a_format_change_not_value_changes(v2, v3):
    r = compare_tables(v2, v3, max_detail_rows=10_000)
    assert [(c.column, c.old_type, c.new_type) for c in r.schema_diff.type_changes] == [
        ("amount", ColumnType.INTEGER, ColumnType.FLOAT)
    ]
    tc = r.schema_diff.type_changes[0]
    assert (tc.kind, tc.values_unchanged) == ("format", True)
    kinds = {f.type for f in r.findings if f.column == "amount"}
    assert "column_format_changed" in kinds and "column_type_changed" not in kinds
    assert "values unchanged" in r.summary
    assert r.rows.modified == 0
    assert "amount" not in r.rows.modified_by_column
    assert sorted(int(k) for k in r.rows.removed_keys) == TRUTH["v2_to_v3"]["removed_ids"]
    assert r.rows.added == 0


def test_v2_v3_drift_is_stable_everywhere(v2, v3):
    r = compare_tables(v2, v3)
    bands = {d.column: d.band for d in r.drift if d.value is not None}
    assert bands and set(bands.values()) == {"stable"}


def test_reformatting_alone_leaves_the_content_hash_identical(v2):
    """Same data, amount rewritten as currency strings, columns reordered."""
    rows = [dict(r) for r in v2.rows]
    reformatted = []
    for r in rows:
        r2 = dict(reversed(list(r.items())))
        if r2["amount"].isdigit():
            r2["amount"] = f"${int(r2['amount']):,}.00"
        reformatted.append(r2)
    new = _table(reformatted, "v2b")
    r = compare_tables(v2, new)
    assert r.hashes.identical
    assert r.rows.modified == 0 and r.rows.added == 0 and r.rows.removed == 0


def test_a_single_changed_value_changes_the_hash(v2):
    rows = [dict(r) for r in v2.rows]
    rows[0]["company"] = rows[0]["company"] + " X"
    r = compare_tables(v2, _table(rows, "v2c"))
    assert not r.hashes.identical
    assert r.rows.modified == 1
    assert r.rows.modified_rows[0].changed_fields[0].column == "company"


# ---------------------------------------------------- keys and fallbacks


def test_bad_requested_key_is_an_error(v1, v2):
    with pytest.raises(ComparisonError):
        compare_tables(v1, v2, key_column="company")  # not unique
    with pytest.raises(ComparisonError):
        compare_tables(v1, v2, key_column="nope")


def test_no_key_falls_back_to_row_hash_and_says_so():
    old = _table([{"a": str(i), "b": "x"} for i in range(50)])
    new = _table([{"a": str(i), "b": "x"} for i in range(5, 60)])
    r = compare_tables(old, new)
    assert r.key_column is None
    assert r.rows.matching == "by_row_hash"
    assert r.rows.modified is None
    assert (r.rows.unchanged, r.rows.added, r.rows.removed) == (45, 10, 5)
    assert r.warnings and "key" in r.warnings[0].lower()


def test_rename_detection_without_a_key_uses_value_overlap():
    old = _table([{"a": str(i), "phone": f"555-{i:04d}"} for i in range(60)])
    new = _table([{"a": str(i), "phone_number": f"555-{i:04d}"} for i in range(60)])
    r = compare_tables(old, new)
    assert [(x.old_name, x.new_name) for x in r.schema_diff.renamed] == [("phone", "phone_number")]
    assert r.schema_diff.removed_columns == [] and r.schema_diff.added_columns == []


def test_unrelated_added_and_removed_columns_are_not_called_a_rename():
    old = _table([{"id": str(i), "zip": f"{10000 + i}"} for i in range(60)])
    new = _table([{"id": str(i), "industry": f"I{i % 7}"} for i in range(60)])
    r = compare_tables(old, new)
    assert r.schema_diff.renamed == []
    assert r.schema_diff.removed_columns == ["zip"]
    assert r.schema_diff.added_columns == ["industry"]


def test_detail_lists_are_capped_but_counts_are_exact(v1, v2):
    r = compare_tables(v1, v2, max_detail_rows=5)
    assert r.rows.modified == 60 and len(r.rows.modified_rows) == 5
    assert r.rows.added == 250 and len(r.rows.added_keys) == 5
    assert r.rows.detail_truncated


def test_identifier_columns_are_not_drift_tested(v1, v2):
    r = compare_tables(v1, v2)
    ident = next(d for d in r.drift if d.column == "id")
    assert ident.value is None and "identifier" in ident.not_evaluated_reason


def test_findings_are_structured(v1, v2):
    r = compare_tables(v1, v2)
    by_type = {f.type for f in r.findings}
    assert {"distribution_drift", "volume_change", "null_rate_change", "content_hash"} <= by_type
    drift = [f for f in r.findings if f.type == "distribution_drift"]
    assert all(f.detector_type == "statistical" and f.metric == "psi" for f in drift)
    h = next(f for f in r.findings if f.type == "content_hash")
    assert h.detector_type == "integrity"
    assert "who or why" in h.message


# ------------------------------------------------------------- profiler


def test_profile_reports_counts_rates_and_numeric_summary(v1):
    p = profile_table(v1)
    assert p.row_count == 1000 and p.column_count == 11
    amount = next(c for c in p.columns if c.name == "amount")
    assert amount.numeric is not None
    assert amount.numeric.count > 900
    assert amount.numeric.min <= amount.numeric.median <= amount.numeric.max
    assert math.isclose(sum(amount.numeric.hist_counts) + amount.numeric.below_range + amount.numeric.above_range,
                        amount.numeric.count)
    phone = next(c for c in p.columns if c.name == "phone")
    assert phone.null_rate == 0.0


def test_profile_never_reports_values_for_personal_columns(v1):
    p = profile_table(v1)
    for name in ("email", "phone", "first_name", "last_name"):
        col = next(c for c in p.columns if c.name == name)
        assert col.top_values is None
        assert col.top_values_withheld_reason
    country = next(c for c in p.columns if c.name == "country_code")
    assert country.top_values and {v.value for v in country.top_values} <= {"US", "CA", "GB", "IN", "DE"}


def test_profile_serialized_contains_no_email_addresses(v1):
    blob = profile_table(v1).model_dump_json()
    assert "@" not in blob


def test_profile_handles_empty_and_all_blank_columns():
    t = _table([{"a": "", "b": "1"}, {"a": "", "b": "2"}])
    p = profile_table(t)
    a = next(c for c in p.columns if c.name == "a")
    assert a.null_rate == 1.0 and a.numeric is None and a.top_values_withheld_reason == "no values"


def test_numeric_type_change_with_changed_values_stays_a_type_change():
    old = _table([{"id": "1", "amount": "10"}, {"id": "2", "amount": "20"}], types={"id": ColumnType.INTEGER, "amount": ColumnType.INTEGER})
    new = _table([{"id": "1", "amount": "10.5"}, {"id": "2", "amount": "20.0"}], types={"id": ColumnType.INTEGER, "amount": ColumnType.FLOAT})
    r = compare_tables(old, new)
    tc = r.schema_diff.type_changes[0]
    assert (tc.kind, tc.values_unchanged) == ("type", False)
    assert any(f.type == "column_type_changed" for f in r.findings)


def test_type_change_without_a_key_is_never_called_a_format_change():
    old = _table([{"a": "x", "amount": "10"}, {"a": "y", "amount": "20"}], types={"amount": ColumnType.INTEGER})
    new = _table([{"a": "x", "amount": "10.0"}, {"a": "y", "amount": "20.0"}], types={"amount": ColumnType.FLOAT})
    r = compare_tables(old, new)
    assert r.rows.matching == "by_row_hash"
    assert r.schema_diff.type_changes
    for tc in r.schema_diff.type_changes:
        assert (tc.kind, tc.values_unchanged) == ("type", None)

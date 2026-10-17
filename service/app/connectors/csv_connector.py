from __future__ import annotations

import csv
from pathlib import Path

from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.canonical.type_conformity import (
    DEFAULT_NULL_SENTINELS,
    is_null_sentinel,
    looks_like_date,
    parse_boolean,
    parse_integer_strict,
    parse_numeric,
)
from app.connectors.base import Connector

# A column is classified INTEGER/FLOAT only when at least this fraction
# of its non-blank values conform — see _infer_type. Below this, the
# column is left as STRING rather than forcing a type that most of its
# own values don't actually match; a caller investigating validity gets
# a column-level note about the mixed content instead (see
# app/checks/type_consistency.py), not a wall of per-row flags.
_TYPE_MAJORITY_THRESHOLD = 0.9

# Null-sentinel normalization (below) is gated to columns that LEAN
# numeric or date — a lower bar than _TYPE_MAJORITY_THRESHOLD, since
# this is a narrower question ("does a sentinel string in THIS column
# plausibly mean 'missing'?") than full type classification. A code
# review reproduced the global version of this as a real defect: "NA"
# is Namibia's ISO country code, "None" is a legitimate value in an
# allergies or discount-type field, and "-" is a meaningful placeholder
# in some schemas — normalizing those to blank in a column where they're
# genuine category values manufactures a completeness failure out of
# clean data. A column majority-numeric/date, by contrast, is exactly
# the case "N/A"/"-"/"None" scattered through it clearly do mean
# missing, not a real category value.
_SENTINEL_ELIGIBLE_THRESHOLD = 0.5


def _infer_semantic_hint(column_name: str) -> str | None:
    """CSV has no native field-type metadata, unlike Salesforce or a SQL
    schema, so we fall back to a simple name-based heuristic. This is
    intentionally permissive/best-effort — a wrong guess here only
    means a format check runs (or doesn't run) on a field, not that
    data gets mis-flagged, since the checks themselves stay permissive
    too. A richer connector (Salesforce field types) should set this
    from real metadata instead of guessing from the name."""
    name = column_name.lower()
    if "email" in name:
        return "email"
    if "phone" in name:
        return "phone"
    if "date" in name:
        return "date"
    return None


def _blank_ratio(values: list[str]) -> float:
    if not values:
        return 0.0
    blanks = sum(1 for v in values if v in ("", None))
    return blanks / len(values)


def _leans_numeric_or_date(values: list[str], sentinels: frozenset[str]) -> bool:
    """True when a MAJORITY of this column's non-blank, non-sentinel
    raw values are numeric- or date-shaped — the gate for whether a
    sentinel string found in this column plausibly means "missing"
    (see _SENTINEL_ELIGIBLE_THRESHOLD's docstring for why this needs to
    be a per-column decision, not a global rule). Deliberately looks
    only at non-sentinel values: a column that's otherwise ALL "N/A"
    (no real values to judge by) is left ineligible rather than guessed
    at either way — nothing in that column tells us what the sentinel
    means, so the safer default is to leave it alone."""
    candidates = [v for v in values if v not in ("", None) and not is_null_sentinel(v, sentinels)]
    if not candidates:
        return False
    n = len(candidates)
    numeric_like = sum(1 for v in candidates if parse_numeric(v) is not None)
    date_like = sum(1 for v in candidates if looks_like_date(v))
    return (numeric_like / n >= _SENTINEL_ELIGIBLE_THRESHOLD) or (
        date_like / n >= _SENTINEL_ELIGIBLE_THRESHOLD
    )


def _infer_type(values: list[str]) -> ColumnType:
    """Type inference from a column's raw string values.

    Two deliberate fixes over the original version (a code review
    reproduced the gap these close — "probe 5": a column that read as
    clean INTEGER from an early sample could drift to a non-numeric
    value later with nothing catching it):

    1. Looks at the WHOLE column's non-blank values, not just the first
       50 — a column is no longer at the mercy of where in the file a
       drifted value happens to land.
    2. Uses a MAJORITY threshold (_TYPE_MAJORITY_THRESHOLD, 90%) instead
       of requiring every single value to parse — a column that's 95%
       clean integers and 5% genuinely bad values is still INTEGER,
       with that 5% left for TypeConsistencyCheck (app/checks/
       type_consistency.py) to flag under validity. A column with too
       much non-conforming content to call it one type (below the
       threshold) is left as STRING rather than forcing a type most of
       its own values don't match.

    Boolean inference stays strict (ALL values must match) rather than
    majority-based — it's a narrower, more confident call, and nothing
    asked for it to be loosened. `parse_boolean`/`parse_integer_strict`/
    `parse_numeric` are shared with TypeConsistencyCheck and
    OutlierCheck (app/canonical/type_conformity.py) so "what counts as
    this type" is answered identically everywhere, instead of three
    separate ad hoc parsing spots that could disagree."""
    sample = [v for v in values if v not in ("", None)]
    if not sample:
        return ColumnType.UNKNOWN

    if all(parse_boolean(v) is not None for v in sample):
        return ColumnType.BOOLEAN

    n = len(sample)
    integer_conforming = sum(1 for v in sample if parse_integer_strict(v) is not None)
    if integer_conforming / n >= _TYPE_MAJORITY_THRESHOLD:
        return ColumnType.INTEGER

    numeric_conforming = sum(1 for v in sample if parse_numeric(v) is not None)
    if numeric_conforming / n >= _TYPE_MAJORITY_THRESHOLD:
        return ColumnType.FLOAT

    return ColumnType.STRING


class CSVConnector(Connector):
    """Reads a local CSV file and normalizes it into a CanonicalTable.
    `settings["file_path"]` is required in the connector config."""

    def test_connection(self) -> bool:
        path = Path(self.config.settings.get("file_path", ""))
        return path.is_file()

    def extract(self) -> list[CanonicalTable]:
        path = Path(self.config.settings["file_path"])
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            raw_rows = list(reader)

        # Null-sentinel normalization — "N/A", "NA", "null", "-", etc.
        # mean "missing," not "a value of the wrong type," IN A COLUMN
        # WHERE THAT'S WHAT THEY'D PLAUSIBLY MEAN. Normalizing them to
        # blank means completeness catches them like any other missing
        # cell, and type inference below isn't fooled by them into
        # reading a column as STRING/UNKNOWN just because it's peppered
        # with sentinel text — but only in a column that LEANS numeric
        # or date (see _SENTINEL_ELIGIBLE_THRESHOLD above): "NA" is
        # Namibia's country code, "None" is a legitimate allergy/
        # discount-type answer, and "-" is a meaningful placeholder in
        # plenty of real schemas, so blanking them UNCONDITIONALLY, in
        # every column, manufactured completeness failures out of clean
        # categorical data. Eligibility is decided from the RAW
        # (pre-normalization) values, per column, before any blanking
        # happens. Configurable per connector instance
        # (settings["null_sentinels"]); defaults to DEFAULT_NULL_SENTINELS
        # otherwise.
        configured_sentinels = self.config.settings.get("null_sentinels")
        sentinels = (
            frozenset(s.lower() for s in configured_sentinels)
            if configured_sentinels is not None
            else DEFAULT_NULL_SENTINELS
        )

        raw_columns: dict[str, list[str]] = {name: [] for name in fieldnames}
        for row in raw_rows:
            for name in fieldnames:
                raw_columns[name].append(row.get(name, "") or "")

        sentinel_eligible_columns = {
            name for name, values in raw_columns.items()
            if _leans_numeric_or_date(values, sentinels)
        }

        rows: list[dict[str, str]] = []
        raw_overrides: dict[int, dict[str, str]] = {}
        for row_index, row in enumerate(raw_rows):
            new_row: dict[str, str] = {}
            for name, value in row.items():
                if name in sentinel_eligible_columns and is_null_sentinel(value, sentinels):
                    raw_overrides.setdefault(row_index, {})[name] = value
                    new_row[name] = ""
                else:
                    new_row[name] = value
            rows.append(new_row)

        columns_raw: dict[str, list[str]] = {name: [] for name in fieldnames}
        for row in rows:
            for name in fieldnames:
                columns_raw[name].append(row.get(name, ""))

        columns = [
            ColumnSchema(
                name=name,
                type=_infer_type(values),
                # Heuristic, not a certainty: a column blank in a small
                # minority of rows looks like *missing required data*
                # (flag it). A column blank in most rows looks like a
                # genuinely optional field (don't flag it). This will
                # get real refinement later (e.g. semantic hints like
                # "email" implying required), but it's a meaningfully
                # better default than "any blank -> nullable", which
                # excused every partially-missing required field from
                # the completeness check entirely.
                nullable=_blank_ratio(values) >= 0.5,
                semantic_hint=_infer_semantic_hint(name),
            )
            for name, values in columns_raw.items()
        ]

        table = CanonicalTable(
            tenant_id=self.config.tenant_id,
            source_id=self.config.source_id,
            table_name=path.stem,
            columns=columns,
            rows=[dict(row) for row in rows],
            raw_overrides=raw_overrides,
        )
        return [table]

"""
Canonical data model.

This is the internal "lingua franca" every connector must translate its
source into. Nothing downstream of this module (checks, agent, scoring)
is allowed to know or care where the data originally came from. If a
field like `salesforce_object_type` ever shows up here, the connector
abstraction has leaked — that's a design smell to catch in review.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ColumnType(str, Enum):
    """Closed set of canonical types. Every connector maps its native
    types down to one of these — this is what keeps checks and the
    rule DSL source-agnostic."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


class ColumnSchema(BaseModel):
    """Describes one column/field in a canonical table."""

    name: str
    type: ColumnType
    nullable: bool = True
    # Optional hint set by a connector or inferred later (e.g. "email",
    # "phone", "currency"). Used by checks to pick relevant validations
    # without hardcoding source-specific field names.
    semantic_hint: str | None = None


class CanonicalTable(BaseModel):
    """A single table/object, normalized to the canonical shape.

    `tenant_id` and `source_id` are required on every table so
    multi-tenancy and provenance are threaded through from the very
    first connector call, not bolted on later.
    """

    tenant_id: str
    source_id: str  # which connector instance this came from
    table_name: str
    columns: list[ColumnSchema]
    rows: list[dict[str, Any]]
    # row_index -> {column_name: original_raw_string}, populated ONLY for
    # a cell a connector normalized away (today: a null-sentinel string
    # like "N/A" blanked at ingest — see csv_connector.py's gated
    # sentinel normalization). `rows` itself always holds the
    # NORMALIZED value (blank, here), since that's what every check and
    # the scorer must see for the normalization to mean anything; this
    # field exists purely so a human-facing view (flagged_rows/
    # security_findings row_data in main.py) can still show "N/A"
    # instead of an unexplained empty cell. Absent for any cell that
    # was never normalized — most cells, most tables.
    raw_overrides: dict[int, dict[str, str]] = Field(default_factory=dict)

    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    def display_row(self, row_index: int) -> dict[str, Any]:
        """The row at `row_index`, with any normalized-away cell shown
        as its original raw string instead of the blank value checks
        and scoring actually operate on. Use this (not `rows[idx]`
        directly) anywhere a row is shown back to a human — a scan
        response's flagged_rows/security_findings, in particular."""
        if row_index < 0 or row_index >= len(self.rows):
            return {}
        row = dict(self.rows[row_index])
        overrides = self.raw_overrides.get(row_index)
        if overrides:
            row.update(overrides)
        return row


class ScanContext(BaseModel):
    """Bundles every table extracted in one scan. Most checks only ever
    look at a single table and can ignore this entirely — it exists
    for checks that inherently need to see across tables, like
    referential integrity (does this foreign key actually point at a
    real row somewhere else). A lone CSV upload will usually produce a
    context with just one table; a SQL connector or a multi-file scan
    can produce several."""

    tables: list[CanonicalTable]

    def get_table(self, name: str) -> CanonicalTable | None:
        for t in self.tables:
            if t.table_name == name:
                return t
        return None

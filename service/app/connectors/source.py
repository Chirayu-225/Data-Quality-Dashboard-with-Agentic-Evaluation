"""The contract every "connect a source" connector implements.

A source (a database, a Google Drive, later Salesforce...) is browsed as a
set of TABLES and read into the same CanonicalTable shape the CSV upload
produces. Everything after extraction (checks, scoring, AI review,
privacy) is shared and knows nothing about where the rows came from.

Rules for implementers (enforced by tests/test_connector_contract.py):
- Raise ConnectorError (or a subclass) for anything the user can act on.
  The message must never contain a password, token or key.
- `list_tables()` returns TableInfo with ids that are unique and stable
  for that source; `extract_selected(ids)` accepts exactly those ids.
- Every cell is a string; missing values are "".
- Honour `row_limit` and report truncation through ExtractedTable.
- Import optional third-party libraries lazily (inside methods) so a
  missing library disables only this connector, never the others.
"""
from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from app.canonical.models import CanonicalTable
from app.connectors.base import Connector

DEFAULT_ROW_LIMIT = 5000
MAX_ROW_LIMIT = 100_000


class ConnectorError(Exception):
    """A problem the user can act on. Safe to show; contains no secrets."""


@dataclass
class ColumnInfo:
    name: str
    type: str


@dataclass
class ForeignKeyInfo:
    columns: list[str]
    referred_table: str
    referred_columns: list[str]


@dataclass
class TableInfo:
    name: str
    approx_rows: int | None
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    id: str | None = None  # opaque, stable id; defaults to the name
    description: str | None = None  # e.g. "Google Sheet tab in 'CRM export'"

    def __post_init__(self) -> None:
        if self.id is None:
            self.id = self.name


@dataclass
class ExtractedTable:
    table: CanonicalTable
    rows_scanned: int
    approx_total_rows: int | None
    ordered_by_primary_key: bool
    skipped_columns: dict[str, str]
    hit_row_cap: bool = False
    # Files keep their own row order, so a re-scan reads the same rows even
    # without a primary key. None = decide from ordered_by_primary_key.
    stable_row_order: bool | None = None
    notes: list[str] = field(default_factory=list)

    def warnings(self) -> list[str]:
        out = []
        name = self.table.table_name
        total = self.approx_total_rows
        if total is not None and total > self.rows_scanned:
            out.append(
                f"'{name}': scanned the first {self.rows_scanned:,} of about {total:,} rows "
                f"(row cap). The score describes that sample, not the whole table."
            )
        elif total is None and self.hit_row_cap:
            out.append(
                f"'{name}': scanned the first {self.rows_scanned:,} rows (row cap); the table may have more. "
                f"The score describes that sample, not the whole table."
            )
        stable = self.ordered_by_primary_key if self.stable_row_order is None else self.stable_row_order
        if not stable:
            out.append(
                f"'{name}' has no primary key, so which rows were scanned can differ between scans."
            )
        for col, why in self.skipped_columns.items():
            out.append(f"'{name}.{col}' was not scanned: {why}.")
        out.extend(self.notes)
        return out


def clamp_row_limit(value: int | str | None, default: int) -> int:
    try:
        v = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        v = default
    return max(1, min(v, MAX_ROW_LIMIT))


class TabularSource(Connector):
    """Base class for browsable sources. See the module docstring."""

    @abstractmethod
    def check(self) -> None:
        """Cheap credentials/reachability check. Raises ConnectorError."""

    @abstractmethod
    def list_tables(self) -> list[TableInfo]:
        ...

    @abstractmethod
    def extract_selected(self, table_ids: list[str], row_limit: int | None = None) -> list[ExtractedTable]:
        ...

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release pooled resources. Default: nothing to release."""

    def revoke(self) -> None:  # noqa: B027 - optional hook
        """Called when the user disconnects (e.g. revoke an OAuth grant).
        Best effort; must not raise."""

    def account_label(self) -> str | None:
        """Optional: who/what this connection is signed in as (shown in UI)."""
        return None

    def test_connection(self) -> bool:
        try:
            self.check()
            return True
        except ConnectorError:
            return False

    def extract(self) -> list[CanonicalTable]:
        ids = [t.id for t in self.list_tables()]
        return [e.table for e in self.extract_selected(ids)]

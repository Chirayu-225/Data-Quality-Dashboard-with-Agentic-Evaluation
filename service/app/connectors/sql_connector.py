"""SQL connector: MySQL, PostgreSQL and SQLite (read-only), via SQLAlchemy.

Why it exists: uploading files by hand is not a product. A connection is
created once (credentials stay on the server, encrypted), the user picks
tables, and the same scorecard runs on them.

What it does:
- discovers tables, columns, primary keys and foreign keys (real metadata)
- reads at most `row_limit` rows per table, ordered by primary key so the
  same table gives the same rows on every scan
- converts every value to the same string-based canonical shape the CSV
  connector produces (NULL becomes blank), so every check, the scorer and
  the AI-review privacy code run unchanged

What it deliberately does NOT do:
- run any SQL the user wrote. Table and column names come from the
  database's own catalog and are used through SQLAlchemy Core, never
  string-built, so there is no injection surface.
- write anything. The session is opened read-only (defence in depth: the
  database user you connect with should ALSO be read-only; that is the
  real guarantee, this is a second lock).
- scan a whole large table. A row cap applies, and the response says how
  many rows were scanned of how many exist.

Known limits, stated plainly:
- The first N rows by primary key are a biased sample (oldest rows).
- A table with no primary key has no stable order, so its sample can differ
  between scans.
- Cross-table checks still guess foreign keys from column names
  (`account_id` -> `accounts`); the real foreign keys are discovered and
  shown but not yet used to drive the check.
- Row counts shown for MySQL and PostgreSQL are the database's estimates.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import logging
import os
from pathlib import Path
from typing import Any

from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.connectors.base import ConnectorConfig
from app.connectors.source import (  # noqa: F401 - re-exported for callers
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    ColumnInfo,
    ConnectorError,
    ExtractedTable,
    ForeignKeyInfo,
    TableInfo,
    TabularSource,
)
from app.connectors.csv_connector import _blank_ratio, _infer_semantic_hint

_log = logging.getLogger(__name__)

DIALECTS = ("mysql", "postgresql", "sqlite")
_DRIVERS = {"mysql": "mysql+pymysql", "postgresql": "postgresql+psycopg2"}
_DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432}

CONNECT_TIMEOUT_S = 10
QUERY_TIMEOUT_S = 30


class SQLConnectorError(ConnectorError):
    """A problem the user can act on. The message never contains the password."""


def sqlite_base_dir() -> Path:
    raw = os.environ.get("CONNECTOR_SQLITE_DIR", "").strip()
    return Path(raw).resolve() if raw else Path("demo_data").resolve()


def _allowed_hosts() -> set[str] | None:
    raw = os.environ.get("CONNECTOR_ALLOWED_HOSTS", "").strip()
    return {h.strip().lower() for h in raw.split(",") if h.strip()} if raw else None


def default_row_limit() -> int:
    raw = os.environ.get("SQL_SCAN_ROW_LIMIT", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ROW_LIMIT
    except ValueError:
        value = DEFAULT_ROW_LIMIT
    return max(1, min(value, MAX_ROW_LIMIT))


def _canonical_type(sa_type: Any) -> ColumnType:
    import sqlalchemy as sa

    if isinstance(sa_type, sa.Boolean):
        return ColumnType.BOOLEAN
    if isinstance(sa_type, sa.Integer):
        return ColumnType.INTEGER
    if isinstance(sa_type, (sa.Numeric, sa.Float)):  # Numeric includes DECIMAL
        return ColumnType.FLOAT
    if isinstance(sa_type, sa.DateTime):
        return ColumnType.DATETIME
    if isinstance(sa_type, sa.Date):
        return ColumnType.DATE
    return ColumnType.STRING


def _is_binary(sa_type: Any) -> bool:
    import sqlalchemy as sa

    return isinstance(sa_type, sa.LargeBinary) or type(sa_type).__name__.upper() in {
        "BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB", "BYTEA", "VARBINARY", "BINARY", "BIT",
    }


def _to_cell(value: Any) -> str:
    """Canonical cell: always a string, NULL -> blank (same as the CSV connector)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ""
    return str(value)


class SQLConnector(TabularSource):
    """`settings`: dialect, host, port, database, username, password, schema
    (optional), row_limit (optional), sqlite_file (sqlite only: a file name
    inside CONNECTOR_SQLITE_DIR)."""

    def __init__(self, config: ConnectorConfig):
        super().__init__(config)
        self._engine = None

    # ---- connection -----------------------------------------------------

    def _settings(self) -> dict[str, Any]:
        s = self.config.settings
        dialect = str(s.get("dialect", "")).lower()
        if dialect not in DIALECTS:
            raise SQLConnectorError(f"Unsupported database type. Choose one of: {', '.join(DIALECTS)}.")
        return s

    def _redact(self, text: str) -> str:
        pw = str(self.config.settings.get("password") or "")
        return text.replace(pw, "***") if pw else text

    def engine(self):
        if self._engine is not None:
            return self._engine
        import sqlalchemy as sa

        s = self._settings()
        dialect = s["dialect"]
        if dialect == "sqlite":
            name = str(s.get("sqlite_file") or "").strip()
            base = sqlite_base_dir()
            path = (base / name).resolve() if name else None
            if path is None or base not in path.parents or not path.is_file():
                raise SQLConnectorError(
                    f"SQLite file not found in the allowed folder ({base.name}/). "
                    "Only files inside that folder can be opened."
                )
            self._engine = sa.create_engine(
                f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", connect_args={"uri": True}
            )
            return self._engine

        host = str(s.get("host") or "").strip()
        database = str(s.get("database") or "").strip()
        username = str(s.get("username") or "").strip()
        if not host or not database or not username:
            raise SQLConnectorError("Host, database and username are required.")
        allowed = _allowed_hosts()
        if allowed is not None and host.lower() not in allowed:
            raise SQLConnectorError("That host is not on this server's allowed-hosts list.")
        try:
            port = int(s.get("port") or _DEFAULT_PORTS[dialect])
        except (TypeError, ValueError) as exc:
            raise SQLConnectorError("Port must be a number.") from exc

        url = sa.engine.URL.create(
            _DRIVERS[dialect], username=username, password=s.get("password") or None,
            host=host, port=port, database=database,
        )
        if dialect == "mysql":
            connect_args = {
                "connect_timeout": CONNECT_TIMEOUT_S,
                "read_timeout": QUERY_TIMEOUT_S,
                "write_timeout": QUERY_TIMEOUT_S,
                "init_command": "SET SESSION TRANSACTION READ ONLY",
            }
        else:
            connect_args = {
                "connect_timeout": CONNECT_TIMEOUT_S,
                "options": f"-c default_transaction_read_only=on -c statement_timeout={QUERY_TIMEOUT_S * 1000}",
            }
        self._engine = sa.create_engine(url, connect_args=connect_args, pool_pre_ping=True)
        return self._engine

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def _friendly(self, exc: Exception) -> SQLConnectorError:
        if isinstance(exc, SQLConnectorError):
            return exc
        text = self._redact(str(getattr(exc, "orig", None) or exc))
        low = text.lower()
        if any(k in low for k in ("access denied", "password authentication", "authentication failed")):
            msg = "The database rejected the username or password."
        elif "unknown database" in low or ("database" in low and "does not exist" in low):
            msg = "That database does not exist on the server."
        elif any(k in low for k in ("timed out", "timeout")):
            msg = "The connection timed out. Check the host, port and any firewall."
        elif any(k in low for k in ("can't connect", "connection refused", "could not connect",
                                    "name or service not known", "getaddrinfo", "nodename")):
            msg = "Could not reach the database server. Check the host and port."
        else:
            msg = f"The database returned an error: {text[:200]}"
        return SQLConnectorError(msg)

    def check(self) -> None:
        """Raises SQLConnectorError with a safe, readable message on failure."""
        import sqlalchemy as sa

        try:
            with self.engine().connect() as conn:
                conn.execute(sa.text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise self._friendly(exc) from None

    # ---- discovery ------------------------------------------------------

    def _schema(self) -> str | None:
        s = self.config.settings
        if s.get("dialect") == "postgresql":
            return (s.get("schema") or "public").strip() or "public"
        return None  # MySQL: the database IS the schema; SQLite: none

    def _approx_rows(self, conn) -> dict[str, int]:
        import sqlalchemy as sa

        s = self._settings()
        counts: dict[str, int] = {}
        try:
            if s["dialect"] == "mysql":
                rows = conn.execute(
                    sa.text("SELECT table_name, table_rows FROM information_schema.tables "
                            "WHERE table_schema = DATABASE()")
                )
                counts = {str(r[0]): int(r[1] or 0) for r in rows}
            elif s["dialect"] == "postgresql":
                rows = conn.execute(
                    sa.text("SELECT c.relname, GREATEST(c.reltuples, 0)::bigint FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p')"),
                    {"schema": self._schema()},
                )
                counts = {str(r[0]): int(r[1] or 0) for r in rows}
        except Exception:  # noqa: BLE001 - counts are a nicety, never fatal
            _log.info("row estimate query failed; continuing without estimates")
        return counts

    def list_tables(self) -> list[TableInfo]:
        import sqlalchemy as sa

        try:
            engine = self.engine()
            insp = sa.inspect(engine)
            schema = self._schema()
            names = sorted(insp.get_table_names(schema=schema))
            with engine.connect() as conn:
                estimates = self._approx_rows(conn)
                sqlite = self._settings()["dialect"] == "sqlite"
                out = []
                for name in names:
                    cols = [
                        ColumnInfo(c["name"], str(c["type"]))
                        for c in insp.get_columns(name, schema=schema)
                    ]
                    pk = list(insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
                    fks = [
                        ForeignKeyInfo(
                            list(fk["constrained_columns"]),
                            fk["referred_table"],
                            list(fk["referred_columns"]),
                        )
                        for fk in insp.get_foreign_keys(name, schema=schema)
                    ]
                    approx = estimates.get(name) or None  # 0 = "never analyzed", not "empty"
                    if sqlite:
                        approx = conn.execute(sa.select(sa.func.count()).select_from(sa.table(name))).scalar()
                    out.append(TableInfo(name, approx, cols, pk, fks))
            return out
        except Exception as exc:  # noqa: BLE001
            raise self._friendly(exc) from None

    # ---- extraction -----------------------------------------------------

    def extract_selected(self, table_names: list[str], row_limit: int | None = None) -> list[ExtractedTable]:
        import sqlalchemy as sa

        limit = max(1, min(int(row_limit or self.config.settings.get("row_limit") or default_row_limit()),
                           MAX_ROW_LIMIT))
        try:
            engine = self.engine()
            insp = sa.inspect(engine)
            schema = self._schema()
            existing = set(insp.get_table_names(schema=schema))
            unknown = [t for t in table_names if t not in existing]
            if unknown:
                raise SQLConnectorError(f"Not a table in this database: {', '.join(unknown)}")
            meta = sa.MetaData()
            results: list[ExtractedTable] = []
            with engine.connect() as conn:
                estimates = self._approx_rows(conn)
                for name in table_names:
                    tbl = sa.Table(name, meta, schema=schema, autoload_with=engine)
                    skipped = {c.name: "binary column" for c in tbl.columns if _is_binary(c.type)}
                    cols = [c for c in tbl.columns if c.name not in skipped]
                    if not cols:
                        raise SQLConnectorError(f"'{name}' has no readable columns.")
                    pk = [c for c in tbl.primary_key.columns if c.name not in skipped]
                    stmt = sa.select(*cols)
                    if pk:
                        stmt = stmt.order_by(*pk)
                    rows = [
                        {c.name: _to_cell(v) for c, v in zip(cols, r)}
                        for r in conn.execute(stmt.limit(limit)).fetchall()
                    ]
                    total = estimates.get(name) or None
                    if total is None and self._settings()["dialect"] == "sqlite":
                        total = conn.execute(sa.select(sa.func.count()).select_from(tbl)).scalar()
                    if total is not None and total < len(rows):
                        total = len(rows)  # an estimate can lag; never claim fewer rows than we read
                    columns = [
                        ColumnSchema(
                            name=c.name,
                            type=_canonical_type(c.type),
                            nullable=_blank_ratio([r[c.name] for r in rows]) >= 0.5,
                            semantic_hint=_infer_semantic_hint(c.name),
                        )
                        for c in cols
                    ]
                    results.append(
                        ExtractedTable(
                            table=CanonicalTable(
                                tenant_id=self.config.tenant_id,
                                source_id=self.config.source_id,
                                table_name=name,
                                columns=columns,
                                rows=rows,
                            ),
                            rows_scanned=len(rows),
                            approx_total_rows=total,
                            ordered_by_primary_key=bool(pk),
                            skipped_columns=skipped,
                            hit_row_cap=len(rows) >= limit,
                        )
                    )
            return results
        except Exception as exc:  # noqa: BLE001
            raise self._friendly(exc) from None


# ---- registry ---------------------------------------------------------------

def _sql_specs():
    from app.connectors.registry import ConnectorSpec, FieldSpec

    def network_fields(default_port: int) -> tuple:
        return (
            FieldSpec("host", "Host", required=True, default="127.0.0.1"),
            FieldSpec("port", "Port", kind="number", default=default_port),
            FieldSpec("database", "Database", required=True),
            FieldSpec("username", "Username", required=True,
                      help="Use a read-only user. This app opens read-only sessions as a second lock."),
            FieldSpec("password", "Password", kind="password", secret=True),
        )

    return [
        ConnectorSpec(
            key="mysql", label="MySQL / MariaDB", category="database",
            description="Read-only scan of tables in a MySQL or MariaDB database.",
            fields=network_fields(3306), factory=SQLConnector, fixed_settings=(("dialect", "mysql"),),
        ),
        ConnectorSpec(
            key="postgresql", label="PostgreSQL", category="database",
            description="Read-only scan of tables in one PostgreSQL schema.",
            fields=(*network_fields(5432),
                    FieldSpec("schema", "Schema", default="public", placeholder="public")),
            factory=SQLConnector, fixed_settings=(("dialect", "postgresql"),),
        ),
        ConnectorSpec(
            key="sqlite", label="SQLite file", category="database",
            description="A SQLite file in the server's data folder (demo.db ships with demo data).",
            fields=(FieldSpec("sqlite_file", "Database file", required=True, default="demo.db",
                              help="A file inside the server's data folder."),),
            factory=SQLConnector, fixed_settings=(("dialect", "sqlite"),),
        ),
    ]


CONNECTOR_SPECS = _sql_specs()

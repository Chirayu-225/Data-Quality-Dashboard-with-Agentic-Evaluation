"""SQL connector: SQLite always; MySQL/MariaDB only when a server is reachable
(set TEST_MYSQL_* env vars, defaults match demo_data/demo_mysql.sql)."""
import os
import sqlite3

import pytest

from app.connectors.base import ConnectorConfig
from app.connectors.sql_connector import SQLConnector, SQLConnectorError


def _cfg(**settings):
    return ConnectorConfig(tenant_id="t", source_id="s", settings=settings)


@pytest.fixture
def sqlite_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTOR_SQLITE_DIR", str(tmp_path))
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, company TEXT, revenue REAL);
        CREATE TABLE contacts (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), email TEXT, blob BLOB);
        CREATE TABLE loose (a TEXT, b TEXT);
        INSERT INTO accounts VALUES (1,'Acme',10.5),(2,'Globex',NULL),(3,'Initech',7);
        INSERT INTO contacts VALUES (1,1,'a@acme.com',x'00'),(2,99,NULL,x'01');
        INSERT INTO loose VALUES ('x','y'),('z',NULL);
        """
    )
    con.commit()
    con.close()
    return tmp_path


def _sqlite(**extra):
    return SQLConnector(_cfg(dialect="sqlite", sqlite_file="t.db", **extra))


def test_list_tables_with_keys_and_counts(sqlite_dir):
    c = _sqlite()
    c.check()
    tables = {t.name: t for t in c.list_tables()}
    assert set(tables) == {"accounts", "contacts", "loose"}
    assert tables["accounts"].approx_rows == 3
    assert tables["accounts"].primary_key == ["id"]
    fk = tables["contacts"].foreign_keys[0]
    assert (fk.columns, fk.referred_table) == (["account_id"], "accounts")
    c.close()


def test_extract_blank_for_null_and_strings(sqlite_dir):
    c = _sqlite()
    (acc,) = c.extract_selected(["accounts"])
    rows = acc.table.rows
    assert rows[1]["revenue"] == ""
    assert all(isinstance(v, str) for r in rows for v in r.values())
    assert acc.ordered_by_primary_key and acc.rows_scanned == 3


def test_row_cap_and_warning(sqlite_dir):
    c = _sqlite()
    (acc,) = c.extract_selected(["accounts"], row_limit=2)
    assert acc.rows_scanned == 2
    assert any("row cap" in w for w in acc.warnings())


def test_no_primary_key_warns(sqlite_dir):
    (loose,) = _sqlite().extract_selected(["loose"])
    assert not loose.ordered_by_primary_key
    assert any("no primary key" in w for w in loose.warnings())


def test_binary_column_skipped(sqlite_dir):
    (con,) = _sqlite().extract_selected(["contacts"])
    assert "blob" in con.skipped_columns
    assert "blob" not in con.table.rows[0]
    assert any("blob" in w for w in con.warnings())


def test_unknown_table_rejected(sqlite_dir):
    with pytest.raises(SQLConnectorError):
        _sqlite().extract_selected(["nope"])


@pytest.mark.parametrize("name", ["../t.db", "/etc/passwd", "sub/t.db", "..\\t.db"])
def test_sqlite_path_traversal_blocked(sqlite_dir, name):
    c = SQLConnector(_cfg(dialect="sqlite", sqlite_file=name))
    with pytest.raises(SQLConnectorError):
        c.check()


def test_sqlite_is_read_only(sqlite_dir):
    import sqlalchemy as sa

    c = _sqlite()
    with pytest.raises(Exception):
        with c.engine().begin() as conn:
            conn.execute(sa.text("DELETE FROM accounts"))


def test_unsupported_dialect():
    with pytest.raises(SQLConnectorError):
        SQLConnector(_cfg(dialect="oracle")).check()


def test_host_allowlist(monkeypatch):
    monkeypatch.setenv("CONNECTOR_ALLOWED_HOSTS", "db.internal")
    c = SQLConnector(_cfg(dialect="mysql", host="evil.example", database="d", username="u", password="p"))
    with pytest.raises(SQLConnectorError):
        c.check()


# ---- real MySQL / MariaDB (skipped when no server) -------------------------

MY = dict(
    dialect="mysql",
    host=os.environ.get("TEST_MYSQL_HOST", "127.0.0.1"),
    port=int(os.environ.get("TEST_MYSQL_PORT", "3306")),
    database=os.environ.get("TEST_MYSQL_DB", "scorecard_demo"),
    username=os.environ.get("TEST_MYSQL_USER", "scorecard_ro"),
    password=os.environ.get("TEST_MYSQL_PASSWORD", "readonly_demo_pw"),
)


@pytest.fixture
def mysql():
    c = SQLConnector(_cfg(**MY))
    try:
        c.check()
    except SQLConnectorError:
        pytest.skip("no MySQL/MariaDB demo server reachable")
    yield c
    c.close()


def test_mysql_tables_and_foreign_keys(mysql):
    tables = {t.name: t for t in mysql.list_tables()}
    assert {"accounts", "contacts", "opportunities"} <= set(tables)
    assert tables["contacts"].foreign_keys[0].referred_table == "accounts"


def test_mysql_extract(mysql):
    (acc,) = mysql.extract_selected(["accounts"])
    assert acc.rows_scanned == 12


def test_mysql_write_is_refused(mysql):
    import sqlalchemy as sa

    with pytest.raises(Exception):
        with mysql.engine().begin() as conn:
            conn.execute(sa.text("DELETE FROM accounts"))


def test_mysql_wrong_password_message_has_no_secret():
    c = SQLConnector(_cfg(**{**MY, "password": "wrongpw-SECRET"}))
    try:
        c.check()
    except SQLConnectorError as e:
        assert "wrongpw-SECRET" not in str(e)
    else:
        pytest.skip("no MySQL/MariaDB demo server reachable")


def test_unknown_total_with_cap_hit_still_warns():
    from app.canonical.models import CanonicalTable
    from app.connectors.sql_connector import ExtractedTable

    t = CanonicalTable(tenant_id="t", source_id="s", table_name="big", columns=[], rows=[])
    e = ExtractedTable(t, 5, None, True, {}, hit_row_cap=True)
    assert any("may have more" in w for w in e.warnings())
    assert not ExtractedTable(t, 5, None, True, {}, hit_row_cap=False).warnings()


PG = dict(
    dialect="postgresql",
    host=os.environ.get("TEST_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("TEST_PG_PORT", "5432")),
    database=os.environ.get("TEST_PG_DB", "scorecard_demo"),
    username=os.environ.get("TEST_PG_USER", "scorecard_ro"),
    password=os.environ.get("TEST_PG_PASSWORD", "readonly_demo_pw"),
)


def test_postgres_read_only_and_fk():
    c = SQLConnector(_cfg(**PG))
    try:
        c.check()
    except SQLConnectorError:
        pytest.skip("no PostgreSQL demo server reachable")
    import sqlalchemy as sa

    tables = {t.name: t for t in c.list_tables()}
    assert tables["contacts"].foreign_keys[0].referred_table == "accounts"
    with pytest.raises(Exception):
        with c.engine().begin() as conn:
            conn.execute(sa.text("DELETE FROM accounts"))
    c.close()

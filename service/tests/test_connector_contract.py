"""The contract every connector must meet (app/connectors/source.py).

Each registered connector needs an entry in SOURCES below. A connector that
is installed but has no entry FAILS test_every_connector_has_a_contract_test,
so a new connector (e.g. Salesforce) cannot ship without being held to the
same rules as MySQL, PostgreSQL, SQLite and Google Drive.
"""
import os
import sqlite3

import pytest

from app.connectors import gdrive_connector as gd
from app.connectors.registry import build_registry
from app.connectors.source import ConnectorError, ExtractedTable, TabularSource
from tests.fake_google import REFRESH, FakeGoogle

SECRET_MARKERS = ("readonly_demo_pw", REFRESH, "client-secret-SECRET")


def _sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTOR_SQLITE_DIR", str(tmp_path))
    con = sqlite3.connect(tmp_path / "c.db")
    con.executescript(
        "CREATE TABLE a (id INTEGER PRIMARY KEY, v TEXT, n REAL);"
        "INSERT INTO a VALUES (1,'x',1.5),(2,NULL,2),(3,'z',NULL);"
        "CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id));"
        "INSERT INTO b VALUES (1,1),(2,3);"
    )
    con.commit()
    con.close()
    return {"sqlite_file": "c.db"}


def _server(prefix, dialect, port):
    def make(tmp_path, monkeypatch):
        return {
            "host": os.environ.get(f"TEST_{prefix}_HOST", "127.0.0.1"),
            "port": int(os.environ.get(f"TEST_{prefix}_PORT", str(port))),
            "database": os.environ.get(f"TEST_{prefix}_DB", "scorecard_demo"),
            "username": os.environ.get(f"TEST_{prefix}_USER", "scorecard_ro"),
            "password": os.environ.get(f"TEST_{prefix}_PASSWORD", "readonly_demo_pw"),
        }
    make.needs_server = True
    return make


def _drive(tmp_path, monkeypatch):
    g = FakeGoogle()
    g.add_csv("C1", "a.csv", "id,v\n1,x\n2,\n3,z\n")
    g.add_sheet("S1", "Book", {"b": [["id", "a_id"], ["1", "1"], ["2", "3"]], "c": [["k"], ["1"]]})
    monkeypatch.setattr(gd, "HTTP_TRANSPORT", g.transport())
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-SECRET")
    return {"auth_mode": "oauth", "refresh_token": REFRESH}


SOURCES = {
    "sqlite": _sqlite,
    "mysql": _server("MYSQL", "mysql", 3306),
    "postgresql": _server("PG", "postgresql", 5432),
    "google_drive": _drive,
}

REGISTRY = build_registry()


def test_every_connector_has_a_contract_test():
    missing = set(REGISTRY.specs) - set(SOURCES)
    assert not missing, f"connectors without a contract test: {missing}. Add them to SOURCES."


@pytest.fixture(params=sorted(SOURCES))
def source(request, tmp_path, monkeypatch) -> TabularSource:
    spec = REGISTRY.get(request.param)
    if spec is None:
        pytest.skip(f"{request.param} not installed")
    settings = SOURCES[request.param](tmp_path, monkeypatch)
    src = spec.build(settings)
    try:
        src.check()
    except ConnectorError:
        if getattr(SOURCES[request.param], "needs_server", False):
            pytest.skip(f"no {request.param} test server reachable")
        raise
    yield src
    src.close()


def test_spec_metadata_is_sane():
    for spec in REGISTRY.specs.values():
        assert spec.label and spec.category and spec.description
        keys = [f.key for f in spec.fields]
        assert len(keys) == len(set(keys)), spec.key
        for f in spec.fields:
            if f.secret:
                assert f.kind in ("password", "textarea"), (spec.key, f.key)
            if f.kind == "select":
                assert f.options, (spec.key, f.key)
            for dep, _ in f.show_if:
                assert dep in keys, (spec.key, f.key, dep)


def test_tables_have_unique_stable_ids(source):
    first = source.list_tables()
    assert first, "a contract fixture must expose at least one table"
    ids = [t.id for t in first]
    assert all(isinstance(i, str) and i for i in ids) and len(ids) == len(set(ids))
    assert [t.id for t in source.list_tables()] == ids


def test_extract_shape_and_strings(source):
    ids = [t.id for t in source.list_tables()][:3]
    out = source.extract_selected(ids)
    assert len(out) == len(ids)
    for e in out:
        assert isinstance(e, ExtractedTable)
        cols = [c.name for c in e.table.columns]
        for row in e.table.rows:
            assert list(row) == cols
            assert all(isinstance(v, str) for v in row.values())
        assert all(isinstance(w, str) for w in e.warnings())


def test_row_limit_honoured(source):
    tid = source.list_tables()[0].id
    (e,) = source.extract_selected([tid], row_limit=1)
    assert e.rows_scanned <= 1 and len(e.table.rows) <= 1


def test_unknown_table_is_a_connector_error(source):
    with pytest.raises(ConnectorError):
        source.extract_selected(["__definitely_not_a_table__"])


def test_errors_never_contain_secrets(source):
    try:
        source.extract_selected(["__nope__"])
    except ConnectorError as exc:
        assert not any(m in str(exc) for m in SECRET_MARKERS)

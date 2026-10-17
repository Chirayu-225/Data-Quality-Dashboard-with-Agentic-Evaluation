"""Adding a connector must never compromise installed ones."""
import json

import pytest
from fastapi.testclient import TestClient

from app.connections import STORE
from app.connectors import registry as reg_mod
from app.connectors.registry import (
    BUILTIN_CONNECTOR_MODULES,
    ConnectorSpec,
    FieldSpec,
    build_registry,
    reset_registry_for_tests,
)
from app.connectors.source import ConnectorError
from app.main import app

MEM = "tests.plugin_fixtures.memory_connector"
BROKEN = "tests.plugin_fixtures.broken_connector"
HIJACK = "tests.plugin_fixtures.hijack_connector"
BUILTIN_KEYS = {"mysql", "postgresql", "sqlite", "google_drive"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("API_ACCESS_TOKEN", raising=False)
    STORE.clear()
    yield TestClient(app)
    STORE.clear()
    reset_registry_for_tests(None)


def test_builtins_load_cleanly():
    r = build_registry()
    assert BUILTIN_KEYS <= set(r.specs)
    assert r.problems == []


def test_broken_module_disables_only_itself():
    r = build_registry([*BUILTIN_CONNECTOR_MODULES, BROKEN, MEM])
    assert BUILTIN_KEYS | {"memory_crm"} <= set(r.specs)
    assert [p.module for p in r.problems] == [BROKEN]


def test_new_module_cannot_replace_installed_key():
    r = build_registry([*BUILTIN_CONNECTOR_MODULES, HIJACK])
    assert r.specs["mysql"].label == "MySQL / MariaDB"
    assert "already provided" in r.problems[0].reason
    # ...and load order cannot be abused either: builtins always load first
    assert list(r.specs)[:4] == ["mysql", "postgresql", "sqlite", "google_drive"]


def test_extra_modules_env(monkeypatch):
    monkeypatch.setenv("EXTRA_CONNECTOR_MODULES", f"{MEM}, {BROKEN}, app.connectors.sql_connector")
    r = build_registry()
    assert "memory_crm" in r.specs and BUILTIN_KEYS <= set(r.specs)
    assert len(r.problems) == 1  # sql_connector listed twice is deduplicated, not an error


def test_invalid_key_rejected():
    r = build_registry([])
    bad = ConnectorSpec(key="bad key!", label="x", category="x", description="x", fields=(), factory=object)
    assert r.register(bad, origin="t") is False and "invalid" in r.problems[0].reason


def test_split_settings_rules():
    spec = ConnectorSpec(
        key="t", label="T", category="x", description="x", factory=object,
        fields=(
            FieldSpec("mode", "Mode", kind="select", default="a", options=(("a", "A"), ("b", "B"))),
            FieldSpec("host", "Host", required=True),
            FieldSpec("port", "Port", kind="number", default=5),
            FieldSpec("pw", "Password", kind="password", secret=True),
            FieldSpec("key", "Key", kind="textarea", secret=True, required=True, show_if=(("mode", "b"),)),
        ),
    )
    public, secret = spec.split_settings({"host": "h", "pw": "p", "junk": "dropped"})
    assert public == {"mode": "a", "host": "h", "port": 5} and secret == {"pw": "p"}
    with pytest.raises(ConnectorError, match="Host is required"):
        spec.split_settings({})
    with pytest.raises(ConnectorError, match="Key is required"):
        spec.split_settings({"host": "h", "mode": "b"})
    with pytest.raises(ConnectorError, match="number"):
        spec.split_settings({"host": "h", "port": "x"})
    with pytest.raises(ConnectorError, match="unsupported"):
        spec.split_settings({"host": "h", "mode": "z"})


def test_catalog_lists_only_installed_and_problems(client):
    reset_registry_for_tests(build_registry([*BUILTIN_CONNECTOR_MODULES, BROKEN]))
    body = client.get("/connectors").json()
    keys = {c["key"]: c for c in body["connectors"]}
    assert "salesforce" not in keys  # nothing is advertised that doesn't exist
    assert keys["mysql"]["status"] == "ready"
    assert all(not f["secret"] or f["kind"] in ("password", "textarea") for c in body["connectors"] for f in c["fields"])
    assert body["unavailable"][0]["module"] == BROKEN


def test_new_connector_works_end_to_end_without_touching_others(client, tmp_path, monkeypatch):
    """The Salesforce scenario: install a new connector, use it, and the
    SQLite connector created before it keeps working unchanged."""
    import sqlite3

    monkeypatch.setenv("CONNECTOR_SQLITE_DIR", str(tmp_path))
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO accounts VALUES (1,'A');")
    con.commit(); con.close()

    sqlite_id = client.post("/connections", json={"dialect": "sqlite", "sqlite_file": "t.db"}).json()["id"]
    before = client.post(f"/connections/{sqlite_id}/scan", data={"tables": '["accounts"]'}).json()

    reset_registry_for_tests(build_registry([*BUILTIN_CONNECTOR_MODULES, MEM]))
    bad = client.post("/connections", json={"type": "memory_crm", "settings": {"api_key": "nope"}})
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    r = client.post("/connections", json={"type": "memory_crm", "name": "CRM", "settings": {"api_key": "good-key-SECRET"}})
    assert r.status_code == 200 and "good-key-SECRET" not in r.text
    mem_id = r.json()["id"]
    ids = [t["id"] for t in client.get(f"/connections/{mem_id}/tables").json()["tables"]]
    scan = client.post(f"/connections/{mem_id}/scan", data={"tables": json.dumps(ids)}).json()
    assert scan["cross_table_findings"][0]["from_table"] == "contacts"

    after = client.post(f"/connections/{sqlite_id}/scan", data={"tables": '["accounts"]'}).json()
    assert after["aggregate_score"] == before["aggregate_score"]
    assert after["tables"][0]["flagged_rows"] == before["tables"][0]["flagged_rows"]


def test_connector_bug_is_contained(client):
    reset_registry_for_tests(build_registry([*BUILTIN_CONNECTOR_MODULES, MEM]))
    r = client.post("/connections", json={"type": "memory_crm",
                                          "settings": {"api_key": "good-key-SECRET", "explode": "1"}})
    assert r.status_code == 502 and "Other sources are unaffected" in r.json()["detail"]
    assert "a bug inside" not in r.text
    assert client.get("/connectors").status_code == 200


def test_connection_of_uninstalled_type_is_409_not_500(client):
    reset_registry_for_tests(build_registry([*BUILTIN_CONNECTOR_MODULES, MEM]))
    mem_id = client.post("/connections", json={"type": "memory_crm", "settings": {"api_key": "good-key-SECRET"}}).json()["id"]
    reset_registry_for_tests(build_registry())  # connector removed
    r = client.get(f"/connections/{mem_id}/tables")
    assert r.status_code == 409


def test_unknown_connector_404(client):
    assert client.post("/connections", json={"type": "oracle", "settings": {}}).status_code == 404

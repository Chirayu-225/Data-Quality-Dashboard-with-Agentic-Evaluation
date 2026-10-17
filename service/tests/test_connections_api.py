import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.connections import STORE
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTOR_SQLITE_DIR", str(tmp_path))
    monkeypatch.delenv("API_ACCESS_TOKEN", raising=False)
    con = sqlite3.connect(tmp_path / "t.db")
    con.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, company TEXT, revenue REAL);
        CREATE TABLE contacts (id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), email TEXT);
        INSERT INTO accounts VALUES (1,'Acme',10.5),(2,'Globex',20),(3,'Initech',7);
        INSERT INTO contacts VALUES (1,1,'a@acme.com'),(2,99,'b@x.com'),(3,2,'c@globex.com');
        """
    )
    con.commit()
    con.close()
    STORE.clear()
    yield
    STORE.clear()


def _create(**kw):
    body = {"name": "demo", "dialect": "sqlite", "sqlite_file": "t.db", **kw}
    return client.post("/connections", json=body)


def test_dialects():
    r = client.get("/connections/dialects")
    assert r.status_code == 200 and "sqlite" in r.json()["dialects"]


def test_create_list_delete_never_returns_password():
    r = _create(password="topsecret")
    assert r.status_code == 200
    body = r.json()
    assert "topsecret" not in json.dumps(body) and body["has_password"] is True
    assert "topsecret" not in client.get("/connections").text
    assert client.delete(f"/connections/{body['id']}").status_code == 200
    assert client.delete(f"/connections/{body['id']}").status_code == 404
    assert client.get("/connections").json()["connections"] == []


def test_bad_connection_not_saved():
    r = _create(sqlite_file="missing.db")
    assert r.status_code == 400
    assert client.get("/connections").json()["connections"] == []


def test_traversal_rejected():
    assert _create(sqlite_file="../x.db").status_code == 400


def test_tables_endpoint():
    cid = _create().json()["id"]
    r = client.get(f"/connections/{cid}/tables")
    assert r.status_code == 200
    names = {t["name"]: t for t in r.json()["tables"]}
    assert names["contacts"]["foreign_keys"][0]["references"] == "accounts"
    assert names["accounts"]["approx_rows"] == 3


def test_unknown_connection_404():
    assert client.get("/connections/nope/tables").status_code == 404
    assert client.post("/connections/nope/scan", data={"tables": '["a"]'}).status_code == 404


def test_expired_connection_404(monkeypatch):
    cid = _create().json()["id"]
    monkeypatch.setenv("CONNECTION_TTL_SECONDS", "60")
    STORE._items[cid].created_at = time.time() - 3600
    assert client.get(f"/connections/{cid}/tables").status_code == 404


def test_access_token(monkeypatch):
    monkeypatch.setenv("API_ACCESS_TOKEN", "s3cret")
    assert _create().status_code == 401
    r = client.post("/connections", json={"dialect": "sqlite", "sqlite_file": "t.db"},
                    headers={"X-Access-Token": "s3cret"})
    assert r.status_code == 200
    assert client.get("/connections").status_code == 401
    assert client.get("/connections", headers={"X-Access-Token": "wrong"}).status_code == 401
    assert client.get("/connections", headers={"X-Access-Token": "s3cret"}).status_code == 200


@pytest.mark.parametrize("tables", ["notjson", "[]", '[1]', '"accounts"'])
def test_scan_rejects_bad_table_list(tables):
    cid = _create().json()["id"]
    assert client.post(f"/connections/{cid}/scan", data={"tables": tables}).status_code == 400


def test_scan_unknown_table_400():
    cid = _create().json()["id"]
    assert client.post(f"/connections/{cid}/scan", data={"tables": '["nope"]'}).status_code == 400


def test_scan_returns_schema_scan_with_orphan_fk():
    cid = _create().json()["id"]
    r = client.post(f"/connections/{cid}/scan", data={"tables": '["accounts","contacts"]'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {t["table_name"] for t in body["tables"]} == {"accounts", "contacts"}
    assert body["cross_table_findings"], "orphan account_id=99 should be reported"
    f = body["cross_table_findings"][0]
    assert (f["from_table"], f["from_column"], f["flagged_row_count"]) == ("contacts", "account_id", 1)


def test_scan_row_cap_warning_surfaces():
    cid = _create().json()["id"]
    r = client.post(f"/connections/{cid}/scan", data={"tables": '["accounts"]', "row_limit": "2"})
    assert r.status_code == 200
    assert "row cap" in json.dumps(r.json())


def test_scan_job_matches_sync():
    cid = _create().json()["id"]
    data = {"tables": '["accounts","contacts"]'}
    sync = client.post(f"/connections/{cid}/scan", data=data).json()
    job = client.post(f"/connections/{cid}/scan/jobs", data=data)
    assert job.status_code == 200
    jid = job.json()["job_id"]
    for _ in range(100):
        s = client.get(f"/scans/jobs/{jid}").json()
        if s["status"] in ("completed", "succeeded", "done", "failed"):
            break
        time.sleep(0.1)
    assert s["status"] != "failed", s
    result = s.get("result") or {}
    assert result.get("aggregate_score") is not None
    assert result["aggregate_score"] == sync["aggregate_score"]

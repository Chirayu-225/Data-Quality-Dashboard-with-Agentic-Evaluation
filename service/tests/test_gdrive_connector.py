"""Google Drive connector against an in-process fake of Google's APIs."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import oauth_flow
from app.connections import STORE
from app.connectors import gdrive_connector as gd
from app.connectors.base import ConnectorConfig
from app.connectors.csv_connector import CSVConnector
from app.connectors.gdrive_connector import GoogleDriveConnector, parse_folder_id
from app.connectors.source import ConnectorError
from app.main import app
from tests.fake_google import REFRESH, FakeGoogle

ACCOUNTS_CSV = "id,company,industry,annual_revenue\n1,Acme,Manufacturing,1000\n2,Globex,asdfghjkl,N/A\n3,Initech,Software,-5\n"


@pytest.fixture
def google(monkeypatch):
    g = FakeGoogle()
    g.add_csv("CSV1", "accounts.csv", ACCOUNTS_CSV)
    g.add_sheet("SHEET1", "CRM export", {
        "contacts": [["id", "account_id", "email"], ["1", "1", "a@acme.com"], ["2", "99", "bad-email"]],
        "opportunities": [["id", "account_id", "amount"], ["1", "1", "100"], ["2", "2", "-40"]],
    })
    g.add_sheet("SHEET2", "Leads", {"Sheet1": [["name", "email", "email"], ["Ann", "a@x.com", "b@x.com"], ["", "", ""]]})
    g.add_xlsx("XLSX1", "regions.xlsx", {"regions": [["code", "label"], ["EU", "Europe"], ["US", 12.0]]})
    g.add_csv("OUT1", "outside.csv", "a,b\n1,2\n", parent="OTHERFOLDER000")
    monkeypatch.setattr(gd, "HTTP_TRANSPORT", g.transport())
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret-SECRET")
    return g


def _oauth_source(**settings):
    s = {"auth_mode": "oauth", "refresh_token": REFRESH, **settings}
    return GoogleDriveConnector(ConnectorConfig(tenant_id="t", source_id="s", settings=s))


def test_folder_parsing():
    assert parse_folder_id("https://drive.google.com/drive/folders/1AbC_def-GHIjkl?usp=sharing") == "1AbC_def-GHIjkl"
    assert parse_folder_id("https://drive.google.com/open?id=1AbCdefGHIjk") == "1AbCdefGHIjk"
    assert parse_folder_id("1AbCdefGHIjk") == "1AbCdefGHIjk"
    assert parse_folder_id("") is None
    with pytest.raises(ConnectorError):
        parse_folder_id("not a link!")


def test_check_and_account(google):
    src = _oauth_source(folder="FOLDER123456789")
    src.check()
    assert src.account_label() == "analyst@example.org"


def test_folder_that_is_a_file_rejected(google):
    google.add_csv("CSV1xxxxxxxx", "x.csv", "a\n1\n")
    with pytest.raises(ConnectorError, match="file, not a folder"):
        _oauth_source(folder="CSV1xxxxxxxx").check()


def test_list_tables_names(google):
    tables = {t.name: t for t in _oauth_source(folder="FOLDER123456789").list_tables()}
    # multi-tab sheet -> tab names; single-tab sheet -> file name; csv/xlsx -> file stem
    assert set(tables) == {"accounts", "contacts", "opportunities", "Leads", "regions"}
    assert "outside" not in tables  # folder filter applied
    ids = [t.id for t in tables.values()]
    assert len(ids) == len(set(ids))
    assert tables["contacts"].description.startswith("Google Sheet")


def test_csv_matches_upload_path_exactly(google, tmp_path):
    (got,) = _oauth_source().extract_selected(["csv:CSV1"])
    p = tmp_path / "accounts.csv"
    p.write_text(ACCOUNTS_CSV)
    want = CSVConnector(ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(p)})).extract()[0]
    assert got.table.rows == want.rows
    assert [(c.name, c.type, c.nullable) for c in got.table.columns] == [(c.name, c.type, c.nullable) for c in want.columns]
    assert got.table.raw_overrides == want.raw_overrides  # "N/A" handled the same way
    assert got.stable_row_order and not any("primary key" in w for w in got.warnings())


def test_sheet_tab_and_header_dedupe_and_blank_rows(google):
    src = _oauth_source()
    ids = {t.name: t.id for t in src.list_tables()}
    contacts, leads = src.extract_selected([ids["contacts"], ids["Leads"]])
    assert contacts.table.table_name == "contacts" and len(contacts.table.rows) == 2
    assert contacts.table.rows[1] == {"id": "2", "account_id": "99", "email": "bad-email"}
    assert [c.name for c in leads.table.columns] == ["name", "email", "email_2"]
    assert len(leads.table.rows) == 1  # trailing blank row dropped


def test_xlsx_sheet(google):
    src = _oauth_source()
    (reg,) = src.extract_selected([t.id for t in src.list_tables() if t.name == "regions"])
    assert reg.table.rows == [{"code": "EU", "label": "Europe"}, {"code": "US", "label": "12"}]


def test_row_cap(google):
    src = _oauth_source()
    (acc,) = src.extract_selected(["csv:CSV1"], row_limit=2)
    assert acc.rows_scanned == 2 and acc.hit_row_cap
    assert any("row cap" in w for w in acc.warnings())
    (acc,) = src.extract_selected(["csv:CSV1"], row_limit=3)
    assert not acc.hit_row_cap


def test_unknown_id(google):
    with pytest.raises(ConnectorError):
        _oauth_source().extract_selected(["csv:NOPE"])


@pytest.mark.parametrize("status,needle", [(403, "denied"), (404, "not found"), (429, "rate limit"), (500, "HTTP 500")])
def test_google_errors_are_readable(google, status, needle):
    google.fail_status = status
    with pytest.raises(ConnectorError, match=needle):
        _oauth_source().check()


def test_revoked_refresh_token(google):
    google.token_ok = False
    with pytest.raises(ConnectorError, match="expired or was revoked"):
        _oauth_source().check()


def test_service_account(google):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    info = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
            "private_key": pem, "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token"}
    src = GoogleDriveConnector(ConnectorConfig(tenant_id="t", source_id="s", settings={
        "auth_mode": "service_account", "service_account_json": json.dumps(info)}))
    src.check()
    assert src.account_label() == "bot@proj.iam.gserviceaccount.com"
    bad = GoogleDriveConnector(ConnectorConfig(tenant_id="t", source_id="s", settings={
        "auth_mode": "service_account", "service_account_json": "{not json"}))
    with pytest.raises(ConnectorError, match="JSON"):
        bad.check()


def test_oauth_not_configured(monkeypatch, google):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID")
    client = TestClient(app)
    r = client.post("/connectors/google_drive/oauth/start", json={"settings": {}})
    assert r.status_code == 400 and "not set up" in r.json()["detail"]
    cat = {c["key"]: c for c in client.get("/connectors").json()["connectors"]}
    assert cat["google_drive"]["oauth_configured"] is False


# ---- full browser sign-in flow through the API ---------------------------------

@pytest.fixture
def api(google, tmp_path, monkeypatch):
    monkeypatch.delenv("API_ACCESS_TOKEN", raising=False)
    STORE.clear()
    oauth_flow.PENDING.clear()
    yield TestClient(app)
    STORE.clear()


def _sign_in(api, folder="https://drive.google.com/drive/folders/FOLDER123456789"):
    r = api.post("/connectors/google_drive/oauth/start", json={"name": "My Drive", "settings": {"folder": folder}})
    assert r.status_code == 200, r.text
    auth = urlparse(r.json()["authorization_url"])
    q = parse_qs(auth.query)
    assert auth.netloc == "accounts.google.com"
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    state = q["state"][0]
    cb = api.get("/connectors/oauth/callback", params={"state": state, "code": "auth-code"}, follow_redirects=False)
    return state, cb


def test_oauth_flow_end_to_end(api, google):
    state, cb = _sign_in(api)
    assert cb.status_code == 303
    loc = urlparse(cb.headers["location"])
    assert loc.path == "/connect"
    conn_id = parse_qs(loc.query)["connection"][0]

    listing = api.get("/connections").text
    assert REFRESH not in listing and "client-secret" not in listing
    conn = api.get("/connections").json()["connections"][0]
    assert conn["type"] == "google_drive" and conn["account"] == "analyst@example.org"
    assert conn["settings"]["folder"].endswith("FOLDER123456789")

    tables = api.get(f"/connections/{conn_id}/tables").json()["tables"]
    by_name = {t["name"]: t["id"] for t in tables}
    r = api.post(f"/connections/{conn_id}/scan", data={
        "tables": json.dumps([by_name["accounts"], by_name["contacts"], by_name["opportunities"]]),
        "custom_instructions": json.dumps({"accounts": "industry must be a real industry name"}),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert {t["table_name"] for t in body["tables"]} == {"accounts", "contacts", "opportunities"}
    orphans = {(f["from_table"], f["from_column"]) for f in body["cross_table_findings"]}
    assert ("contacts", "account_id") in orphans
    assert REFRESH not in r.text

    # state is single-use
    again = api.get("/connectors/oauth/callback", params={"state": state, "code": "x"}, follow_redirects=False)
    assert "oauth_error" in again.headers["location"]

    # disconnect revokes the Google grant
    assert api.delete(f"/connections/{conn_id}").status_code == 200
    assert google.revoked == [REFRESH]


def test_oauth_cancelled_and_bad_state(api):
    r = api.post("/connectors/google_drive/oauth/start", json={"settings": {}})
    state = parse_qs(urlparse(r.json()["authorization_url"]).query)["state"][0]
    cb = api.get("/connectors/oauth/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert "oauth_error=Sign-in+was+cancelled" in cb.headers["location"]
    cb = api.get("/connectors/oauth/callback", params={"state": "forged", "code": "x"}, follow_redirects=False)
    assert "oauth_error" in cb.headers["location"]
    assert api.get("/connections").json()["connections"] == []


def test_oauth_without_refresh_token_not_saved(api, google):
    google.give_refresh_token = False
    _, cb = _sign_in(api)
    assert "oauth_error" in cb.headers["location"]
    assert api.get("/connections").json()["connections"] == []


def test_oauth_mode_cannot_be_created_by_form(api):
    r = api.post("/connections", json={"type": "google_drive", "settings": {"auth_mode": "oauth"}})
    assert r.status_code == 400 and "sign-in" in r.json()["detail"]


def test_service_account_via_form_keeps_key_secret(api):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    key_json = json.dumps({"client_email": "bot@p.iam.gserviceaccount.com", "private_key": pem,
                           "token_uri": "https://oauth2.googleapis.com/token"})
    r = api.post("/connections", json={"type": "google_drive", "name": "SA", "settings": {
        "auth_mode": "service_account", "service_account_json": key_json}})
    assert r.status_code == 200, r.text
    assert "PRIVATE KEY" not in r.text and "PRIVATE KEY" not in api.get("/connections").text
    assert r.json()["has_secret"] is True

"""An in-process fake of the Google endpoints the Drive connector calls
(token, Drive v3, Sheets v4), served through httpx.MockTransport."""
from __future__ import annotations

import io
import json
import re
from urllib.parse import parse_qs, unquote

import httpx

TOKEN = "ya29.fake-access-token"
REFRESH = "1//fake-refresh-token-SECRET"


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class FakeGoogle:
    def __init__(self):
        self.files = {}  # id -> dict(name, mimeType, parents, content|sheets)
        self.calls: list[str] = []
        self.revoked: list[str] = []
        self.fail_status: int | None = None
        self.token_ok = True
        self.give_refresh_token = True
        self.email = "analyst@example.org"
        self.add_folder("FOLDER123456789", "CRM")

    # -- fixtures --
    def add_folder(self, fid, name):
        self.files[fid] = {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.folder", "parents": []}

    def add_csv(self, fid, name, text, parent="FOLDER123456789"):
        self.files[fid] = {"id": fid, "name": name, "mimeType": "text/csv", "parents": [parent],
                           "content": text.encode(), "size": str(len(text))}

    def add_sheet(self, fid, name, tabs: dict[str, list[list]], parent="FOLDER123456789"):
        self.files[fid] = {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.spreadsheet",
                           "parents": [parent], "tabs": tabs}

    def add_xlsx(self, fid, name, sheets, parent="FOLDER123456789"):
        data = xlsx_bytes(sheets)
        self.files[fid] = {"id": fid, "name": name,
                           "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           "parents": [parent], "content": data, "size": str(len(data))}

    # -- server --
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(f"{req.method} {req.url.path}")
        if req.url.host == "oauth2.googleapis.com":
            form = parse_qs(req.content.decode())
            if req.url.path == "/revoke":
                self.revoked.append(form["token"][0])
                return httpx.Response(200)
            if not self.token_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            gt = form["grant_type"][0]
            if gt == "authorization_code":
                assert form.get("code_verifier"), "PKCE verifier missing"
                body = {"access_token": TOKEN, "expires_in": 3599,
                        "scope": "https://www.googleapis.com/auth/drive.readonly"}
                if self.give_refresh_token:
                    body["refresh_token"] = REFRESH
                return httpx.Response(200, json=body)
            if gt == "refresh_token":
                return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3599})
            if gt.endswith("jwt-bearer"):
                return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3599})
            return httpx.Response(400)
        if req.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401)
        if self.fail_status:
            body = {"error": {"errors": [{"reason": "forbidden"}]}}
            return httpx.Response(self.fail_status, json=body)
        p = req.url.path
        q = dict(req.url.params)
        if p == "/drive/v3/about":
            return httpx.Response(200, json={"user": {"emailAddress": self.email}})
        if p == "/drive/v3/files":
            m = re.search(r"'([A-Za-z0-9_-]+)' in parents", q.get("q", ""))
            items = [f for f in self.files.values() if f["mimeType"] != "application/vnd.google-apps.folder"]
            if m:
                items = [f for f in items if m.group(1) in f["parents"]]
            return httpx.Response(200, json={"files": [
                {k: f[k] for k in ("id", "name", "mimeType", "size") if k in f} for f in items]})
        m = re.fullmatch(r"/drive/v3/files/([^/]+)", p)
        if m:
            f = self.files.get(unquote(m.group(1)))
            if f is None:
                return httpx.Response(404)
            if q.get("alt") == "media":
                return httpx.Response(200, content=f["content"])
            return httpx.Response(200, json={k: f[k] for k in ("id", "name", "mimeType")})
        m = re.fullmatch(r"/v4/spreadsheets/([^/]+)", p)
        if m:
            f = self.files.get(m.group(1))
            if f is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"sheets": [
                {"properties": {"sheetId": i, "title": t, "sheetType": "GRID"}} for i, t in enumerate(f["tabs"])]})
        m = re.fullmatch(r"/v4/spreadsheets/([^/]+)/values/(.+)", p)
        if m:
            f = self.files[m.group(1)]
            rng = unquote(m.group(2))
            title_m = re.match(r"'((?:[^']|'')*)'!1:(\d+)", rng)
            title = title_m.group(1).replace("''", "'")
            n = int(title_m.group(2))
            rows = [[str(c) for c in r] for r in f["tabs"][title][:n]]
            return httpx.Response(200, json={"range": rng, "values": rows})
        return httpx.Response(404, json={"unhandled": url})

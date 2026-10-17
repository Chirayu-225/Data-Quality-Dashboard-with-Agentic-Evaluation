# Connectors

A connector lets someone point the scorecard at where their data already
lives instead of uploading files. Installed today: MySQL/MariaDB,
PostgreSQL, SQLite and Google Drive. Salesforce is not built yet.

## Why adding a connector can't break the others

| Guarantee | How it's enforced |
|---|---|
| A new connector needs no edits to the API, the frontend or other connectors | The API (`/connectors`, `/connections/...`) and the Connect screen are generated from each connector's spec (fields, secrets, sign-in type). |
| A broken connector disables only itself | Each module is imported in its own `try/except` (`app/connectors/registry.py`). A failure shows up under `unavailable` in `GET /connectors`; every other connector keeps working. |
| A new connector can't take over an existing one | Keys are first-come. A module that registers `mysql` again is ignored and reported. Built-in modules always load first. |
| A connector bug can't take the API down or leak internals | Every connector call goes through `_call_connector` in `app/main.py`: `ConnectorError` becomes a 400 with its message, and anything else becomes a generic 502. |
| Optional libraries stay optional | Connectors import their third-party libraries inside methods, so a missing library only affects that connector. |
| Every connector meets the same rules | `tests/test_connector_contract.py` runs the same checks against every registered connector. It fails if a connector has no contract fixture. |
| Saved connections keep working | Secrets are stored per connection and opened only by that connection's own connector. A saved connection whose connector was removed returns 409, not 500. |

## Adding one (e.g. Salesforce)

1. Create `app/connectors/salesforce_connector.py` with a class extending
   `TabularSource` (`app/connectors/source.py`). Implement:
   - `check()`
   - `list_tables()`, returning `TableInfo` with stable, unique ids
   - `extract_selected(ids, row_limit)`, returning `ExtractedTable` with string cells and `""` for missing values

   Raise `ConnectorError` for anything the user can fix, and never put secrets in messages.
2. Export `CONNECTOR_SPECS = [ConnectorSpec(key="salesforce", ..., fields=(...), factory=...)]`.
   - Mark passwords, keys and tokens with `secret=True`.
   - For browser sign-in, pass an `OAuthProvider` (see the Google one in `gdrive_connector.py`). The shared `/connectors/{key}/oauth/start` and `/connectors/oauth/callback` endpoints do the rest.
3. Add the module path to `BUILTIN_CONNECTOR_MODULES`, or set `EXTRA_CONNECTOR_MODULES` without editing code.
4. Add a fixture to `SOURCES` in `tests/test_connector_contract.py`, plus connector-specific tests. For an HTTP API, use a fake like `tests/fake_google.py`.
5. Run the full suite. The existing connector tests must pass unchanged.

## Google Drive setup

Reads CSV files, Google Sheets (each tab becomes a table) and Excel
workbooks (each sheet becomes a table). The first row is the header. Rows
go through the same CSV parser as uploads, so a sheet scores exactly like
the same data uploaded by hand.

**Option A: "Sign in with Google" (OAuth).** One-time setup by the admin:

1. In Google Cloud Console, create a project and enable the **Google Drive API** and the **Google Sheets API**.
2. Under OAuth consent screen, choose External and Testing, and add your Google account as a test user.
3. Under Credentials, create an OAuth client ID of type **Web application**, with this authorised redirect URI: `http://localhost:8000/connectors/oauth/callback`
4. Set these before starting the backend:

   ```
   set GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
   set GOOGLE_OAUTH_CLIENT_SECRET=...
   ```

   Optional:
   - `OAUTH_REDIRECT_BASE`, default `http://localhost:8000`
   - `FRONTEND_URL`, default `http://localhost:3000`

The scope requested is `drive.readonly`. Only the refresh token is kept,
encrypted, on the server. Disconnecting revokes it at Google. In "Testing"
mode only listed test users can sign in. A public launch needs Google's
verification for this scope.

**Option B: service-account key (no browser sign-in).** Create a service
account, download its JSON key, share the Drive folder with the service
account's e-mail address, and paste the key on the Connect screen.

**Limits:**
- At most 100 files are listed, newest first. Use a folder link to narrow the list.
- CSV files are read up to 30 MB; larger Excel files are refused.
- Only `row_limit` rows of each table are read.

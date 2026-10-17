"""Saved source connections of any connector type ("connect once, scan many").

Credentials live on the server only. The password is encrypted at rest in
memory (Fernet; key from CONNECTION_SECRET_KEY, or a random per-process key
when unset) and is never returned by any endpoint, never logged, and never
sent to an AI provider.

PoC limits, stated plainly:
- In memory only. Connections vanish on restart (by design for now; a real
  deployment needs a secrets manager and a database for this).
- No user accounts yet. Anyone who can reach the API can use a saved
  connection. Set API_ACCESS_TOKEN to require an `X-Access-Token` header on
  the /connections endpoints, and keep the server on localhost until real
  authentication exists.
- Connections expire after CONNECTION_TTL_SECONDS (default 8 hours).
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet

MAX_CONNECTIONS = 20
_DEFAULT_TTL_SECONDS = 8 * 3600


def _fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        key = os.environ.get("CONNECTION_SECRET_KEY", "").strip()
        _FERNET = Fernet(key.encode() if key else Fernet.generate_key())
    return _FERNET


_FERNET: Fernet | None = None


def ttl_seconds() -> int:
    raw = os.environ.get("CONNECTION_TTL_SECONDS", "").strip()
    try:
        return max(60, int(raw)) if raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS


_LEGACY_SQL_KEYS = ("host", "port", "database", "username", "schema", "sqlite_file", "row_limit")
_SQL_TYPES = {"mysql", "postgresql", "sqlite"}


@dataclass
class SavedConnection:
    """One saved source of any connector type. `settings` are the
    non-secret fields; secrets (passwords, keys, OAuth refresh tokens) are
    one encrypted blob that only `connector_settings()` ever opens."""

    id: str
    name: str
    type: str
    settings: dict
    created_at: float
    _secret_token: bytes | None
    account: str | None = None

    def public(self) -> dict:
        """Everything except the secrets."""
        out = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "settings": dict(self.settings),
            "account": self.account,
            "created_at": self.created_at,
            "has_secret": self._secret_token is not None,
            # kept for clients written against the first (SQL-only) API
            "has_password": self._secret_token is not None,
        }
        if self.type in _SQL_TYPES:
            out["dialect"] = self.type
            for k in _LEGACY_SQL_KEYS:
                out[k] = self.settings.get(k)
        return out

    def connector_settings(self) -> dict:
        secrets_ = json.loads(_fernet().decrypt(self._secret_token)) if self._secret_token else {}
        return {**self.settings, **secrets_}


class ConnectionStoreFull(Exception):
    pass


class ConnectionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, SavedConnection] = {}

    def _expire(self) -> None:
        cutoff = time.time() - ttl_seconds()
        for k in [k for k, v in self._items.items() if v.created_at < cutoff]:
            del self._items[k]

    def add(self, *, name: str, type: str, settings: dict | None = None,
            secrets: dict | None = None, account: str | None = None) -> SavedConnection:
        with self._lock:
            self._expire()
            if len(self._items) >= MAX_CONNECTIONS:
                raise ConnectionStoreFull()
            blob = {k: v for k, v in (secrets or {}).items() if v not in (None, "")}
            conn = SavedConnection(
                id=_secrets.token_urlsafe(24),
                name=name.strip() or f"{type} connection",
                type=type,
                settings=dict(settings or {}),
                created_at=time.time(),
                _secret_token=_fernet().encrypt(json.dumps(blob).encode()) if blob else None,
                account=account,
            )
            self._items[conn.id] = conn
            return conn

    def get(self, conn_id: str) -> SavedConnection | None:
        with self._lock:
            self._expire()
            return self._items.get(conn_id)

    def list(self) -> list[SavedConnection]:
        with self._lock:
            self._expire()
            return sorted(self._items.values(), key=lambda c: c.created_at, reverse=True)

    def delete(self, conn_id: str) -> bool:
        with self._lock:
            return self._items.pop(conn_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


STORE = ConnectionStore()

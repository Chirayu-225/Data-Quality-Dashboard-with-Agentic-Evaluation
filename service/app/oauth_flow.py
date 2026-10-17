"""Short-lived OAuth sign-in attempts (state + PKCE verifier), shared by
every connector that signs in through a browser redirect.

Each attempt is single-use and expires after STATE_TTL_SECONDS. Pending
attempts hold no secrets (the refresh token only exists after the callback,
and then goes straight into the encrypted connection store).
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

STATE_TTL_SECONDS = 600
MAX_PENDING = 100


@dataclass
class PendingSignIn:
    state: str
    connector_key: str
    name: str
    settings: dict
    code_verifier: str
    created_at: float


class PendingStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PendingSignIn] = {}

    def _expire(self) -> None:
        cutoff = time.time() - STATE_TTL_SECONDS
        for k in [k for k, v in self._items.items() if v.created_at < cutoff]:
            del self._items[k]

    def create(self, *, connector_key: str, name: str, settings: dict, code_verifier: str) -> PendingSignIn:
        with self._lock:
            self._expire()
            while len(self._items) >= MAX_PENDING:
                oldest = min(self._items.values(), key=lambda p: p.created_at)
                del self._items[oldest.state]
            p = PendingSignIn(secrets.token_urlsafe(32), connector_key, name, dict(settings),
                              code_verifier, time.time())
            self._items[p.state] = p
            return p

    def pop(self, state: str) -> PendingSignIn | None:
        with self._lock:
            self._expire()
            return self._items.pop(state, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


PENDING = PendingStore()


def redirect_uri() -> str:
    base = os.environ.get("OAUTH_REDIRECT_BASE", "http://localhost:8000").rstrip("/")
    return f"{base}/connectors/oauth/callback"


def frontend_url() -> str:
    return os.environ.get("FRONTEND_URL", "http://localhost:3000").rstrip("/")

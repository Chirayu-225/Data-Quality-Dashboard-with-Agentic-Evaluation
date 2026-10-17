"""Connector registry: the one place the product learns which sources exist.

Design goals (from the mentor's brief: adding a connector must never
compromise the ones already installed):

1. Self-describing. Each connector module exports `CONNECTOR_SPECS`: key,
   label, the form fields it needs (which are secret), and a factory. The
   API and the Connect screen are generated from these specs, so adding a
   connector needs no change to the API, the frontend, or other connectors.
2. Isolated loading. Every module is imported inside its own try/except.
   A connector that fails to import (bug, missing library) is reported as
   unavailable with the reason; every other connector keeps working.
3. No hijacking. Keys are first-come: a later module cannot replace an
   already-registered key (e.g. a new module cannot redefine "mysql").
4. Pluggable without code edits. Extra modules can be listed in
   EXTRA_CONNECTOR_MODULES (comma-separated import paths).
5. Contract-tested. tests/test_connector_contract.py runs against every
   registered connector and fails if a new connector has no contract test.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from app.connectors.base import ConnectorConfig
from app.connectors.source import ConnectorError, TabularSource

_log = logging.getLogger(__name__)

BUILTIN_CONNECTOR_MODULES = (
    "app.connectors.sql_connector",
    "app.connectors.gdrive_connector",
)

# Optional "coming soon" chips (no code behind them yet). Empty on purpose:
# the Connect screen only shows connectors that actually exist.
PLANNED: tuple[dict, ...] = ()

FieldKind = Literal["text", "number", "password", "textarea", "select"]


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    secret: bool = False  # encrypted at rest, never returned by the API
    default: Any = None
    placeholder: str | None = None
    help: str | None = None
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for select
    show_if: tuple[tuple[str, str], ...] = ()  # show only when field==value

    def visible(self, settings: dict[str, Any]) -> bool:
        return all(str(settings.get(k, "")) == v for k, v in self.show_if)

    def public(self) -> dict:
        return {
            "key": self.key, "label": self.label, "kind": self.kind, "required": self.required,
            "secret": self.secret, "default": self.default, "placeholder": self.placeholder,
            "help": self.help, "options": [{"value": v, "label": l} for v, l in self.options],
            "show_if": dict(self.show_if),
        }


class OAuthProvider:
    """Interface for connectors that sign in through a browser redirect."""

    #: settings value that selects the OAuth path (None = always OAuth)
    when: tuple[str, str] | None = None

    def configured(self) -> bool:
        raise NotImplementedError

    def authorization_url(self, *, state: str, redirect_uri: str, code_challenge: str) -> str:
        raise NotImplementedError

    def exchange(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, str]:
        """Returns the secrets to store (e.g. {"refresh_token": ...})."""
        raise NotImplementedError


@dataclass(frozen=True)
class ConnectorSpec:
    key: str
    label: str
    category: str  # "database" | "files" | "crm" ...
    description: str
    fields: tuple[FieldSpec, ...]
    factory: Callable[[ConnectorConfig], TabularSource]
    oauth: OAuthProvider | None = None
    # extra settings always passed to the factory (e.g. {"dialect": "mysql"})
    fixed_settings: tuple[tuple[str, Any], ...] = ()

    @property
    def secret_keys(self) -> set[str]:
        return {f.key for f in self.fields if f.secret}

    def uses_oauth(self, settings: dict[str, Any]) -> bool:
        if self.oauth is None:
            return False
        return self.oauth.when is None or str(settings.get(self.oauth.when[0], "")) == self.oauth.when[1]

    def split_settings(self, raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """Keeps only declared fields, applies defaults, checks required
        fields, and separates secrets from public settings."""
        merged = {f.key: f.default for f in self.fields if f.default is not None}
        merged.update({k: v for k, v in (raw or {}).items() if v not in (None, "")})
        public: dict[str, Any] = {}
        secret: dict[str, str] = {}
        oauth = self.uses_oauth(merged)
        for f in self.fields:
            if not f.visible(merged):
                continue
            value = merged.get(f.key)
            if f.required and value in (None, "") and not (oauth and f.secret):
                raise ConnectorError(f"{f.label} is required.")
            if value in (None, ""):
                continue
            if f.kind == "number":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ConnectorError(f"{f.label} must be a number.") from None
            if f.kind == "select" and f.options and str(value) not in {v for v, _ in f.options}:
                raise ConnectorError(f"{f.label}: unsupported value.")
            if f.secret:
                secret[f.key] = str(value)
            else:
                public[f.key] = value
        return public, secret

    def build(self, settings: dict[str, Any], *, tenant_id: str = "api", source_id: str = "probe") -> TabularSource:
        full = {**settings, **dict(self.fixed_settings)}
        return self.factory(ConnectorConfig(tenant_id=tenant_id, source_id=source_id, settings=full))

    def public(self) -> dict:
        return {
            "key": self.key, "label": self.label, "category": self.category,
            "description": self.description, "status": "ready",
            "fields": [f.public() for f in self.fields],
            "auth": "oauth" if self.oauth and self.oauth.when is None else ("mixed" if self.oauth else "form"),
            "oauth_field": list(self.oauth.when) if self.oauth and self.oauth.when else None,
            "oauth_configured": bool(self.oauth and self.oauth.configured()),
        }


@dataclass
class _LoadProblem:
    module: str
    reason: str


@dataclass
class ConnectorRegistry:
    specs: dict[str, ConnectorSpec] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)
    problems: list[_LoadProblem] = field(default_factory=list)

    def register(self, spec: ConnectorSpec, *, origin: str = "?") -> bool:
        if not spec.key or not spec.key.replace("_", "").isalnum():
            self.problems.append(_LoadProblem(origin, f"invalid connector key {spec.key!r}"))
            return False
        if spec.key in self.specs:
            # First registration wins: a new module can never replace an
            # installed connector.
            self.problems.append(_LoadProblem(
                origin, f"connector key {spec.key!r} already provided by {self.origins[spec.key]}; ignored"))
            _log.warning("ignored duplicate connector key %s from %s", spec.key, origin)
            return False
        self.specs[spec.key] = spec
        self.origins[spec.key] = origin
        return True

    def load_module(self, module_path: str) -> None:
        try:
            module = importlib.import_module(module_path)
            specs = list(getattr(module, "CONNECTOR_SPECS"))
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            self.problems.append(_LoadProblem(module_path, f"{type(exc).__name__}: {exc}"[:300]))
            _log.warning("connector module %s failed to load: %s", module_path, exc)
            return
        for spec in specs:
            if isinstance(spec, ConnectorSpec):
                self.register(spec, origin=module_path)
            else:
                self.problems.append(_LoadProblem(module_path, "CONNECTOR_SPECS item is not a ConnectorSpec"))

    def get(self, key: str) -> ConnectorSpec | None:
        return self.specs.get(key)

    def catalog(self) -> list[dict]:
        out = [s.public() for s in self.specs.values()]
        ready = set(self.specs)
        for p in PLANNED:
            if p["key"] not in ready:
                out.append({**p, "description": "Planned.", "status": "planned", "fields": [],
                            "auth": "form", "oauth_field": None, "oauth_configured": False})
        return out


def _module_list() -> list[str]:
    extra = [m.strip() for m in os.environ.get("EXTRA_CONNECTOR_MODULES", "").split(",") if m.strip()]
    return list(dict.fromkeys([*BUILTIN_CONNECTOR_MODULES, *extra]))


def build_registry(modules: list[str] | None = None) -> ConnectorRegistry:
    reg = ConnectorRegistry()
    for m in modules if modules is not None else _module_list():
        reg.load_module(m)
    return reg


_REGISTRY: ConnectorRegistry | None = None
_LOCK = threading.Lock()


def get_registry() -> ConnectorRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = build_registry()
        return _REGISTRY


def reset_registry_for_tests(registry: ConnectorRegistry | None = None) -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = registry

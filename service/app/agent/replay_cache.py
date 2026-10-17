"""Replay cache for LLM completions.

Purpose: scanning the identical file twice should give the identical
result. Providers say seeded sampling is best-effort, so reproducibility
cannot rest on them. This cache stores each completion under a hash of
everything that determines it (provider, model, system prompt, user
prompt), so a re-scan of identical data replays the same text and the
same flags, spends no quota and sends nothing to the provider.

What it is NOT: it makes results reproducible, not correct. A wrong
answer is replayed as faithfully as a right one.

Privacy: OFF unless LLM_REPLAY_CACHE_DIR is set. Prompts are never
stored, only their SHA-256. The stored completion is the model's reply,
which can quote masked row values in its reasons, so treat the directory
like scan output (see docs/DATA_HANDLING.md).

The prompt-version string is part of the system prompt text, so changing
a prompt changes the key automatically. Changing the model does too.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_STATS = {"hits": 0, "misses": 0, "stores": 0}


def cache_dir() -> Path | None:
    raw = os.environ.get("LLM_REPLAY_CACHE_DIR", "").strip()
    return Path(raw) if raw else None


def make_key(provider: str, model: str, system_prompt: str, user_prompt: str) -> str:
    h = hashlib.sha256()
    for part in (provider, model, system_prompt, user_prompt):
        data = part.encode("utf-8")
        h.update(len(data).to_bytes(8, "big"))  # length-prefix: no ambiguity between fields
        h.update(data)
    return h.hexdigest()


def lookup(key: str) -> str | None:
    d = cache_dir()
    if d is None:
        return None
    path = d / f"{key}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        text = entry["response"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty cached response")
    except (OSError, ValueError, KeyError):
        with _LOCK:
            _STATS["misses"] += 1
        return None
    with _LOCK:
        _STATS["hits"] += 1
    return text


def store(key: str, provider: str, model: str, response: str) -> None:
    d = cache_dir()
    if d is None or not response.strip():
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(
            json.dumps({"provider": provider, "model": model, "created": time.time(), "response": response}),
            encoding="utf-8",
        )
        os.replace(tmp, d / f"{key}.json")  # atomic: a reader never sees a half-written file
        with _LOCK:
            _STATS["stores"] += 1
    except OSError:
        pass  # a cache failure must never fail a scan


def stats() -> dict[str, int]:
    with _LOCK:
        return dict(_STATS)


def reset_stats_for_tests() -> None:
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0

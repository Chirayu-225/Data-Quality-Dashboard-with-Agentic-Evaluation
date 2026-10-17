"""Stability metrics for repeated identical LLM calls.

Pure functions (no network) so the label logic is unit-tested; the live
script scripts/check_live_determinism.py feeds them real replies.

Three separate questions, because they matter differently:
- text:        are the raw replies byte-identical?
- decisions:   do the replies flag the same items (same row_index / value /
               label)? This is what scoring depends on.
- confidence:  for items flagged in every run, how far apart do the
               confidences get?

The label describes THESE prompts on THIS account at THIS moment. It is
not a property of the model, and it says nothing about correctness.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

MIN_RUNS_FOR_LABEL = 5

_ID_KEYS = ("row_index", "value", "from_row_index", "to_row_index")


def _parse(reply: str):
    try:
        data = json.loads(reply)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _items(data: dict) -> list[dict]:
    for key in ("flags", "anomalies", "verifications"):
        if isinstance(data.get(key), list):
            return [i for i in data[key] if isinstance(i, dict)]
    return []


def decision_signature(reply: str):
    """Frozen set of (identity..., label) for every item in the reply, or
    None when the reply is not parseable JSON of a known shape."""
    data = _parse(reply)
    if data is None or not any(k in data for k in ("flags", "anomalies", "verifications")):
        return None
    sig = set()
    for item in _items(data):
        ident = tuple(str(item.get(k)) for k in _ID_KEYS if k in item)
        sig.add(ident + (str(item.get("label", "")),))
    return frozenset(sig)


def confidence_spread(replies: list[str]) -> float | None:
    """Largest max-min confidence gap across runs for any item present in
    every run. None when there is no such item (nothing to compare)."""
    per_run = []
    for r in replies:
        data = _parse(r)
        if data is None:
            return None
        run = {}
        for item in _items(data):
            if "confidence" in item:
                ident = tuple(str(item.get(k)) for k in _ID_KEYS if k in item)
                try:
                    run[ident] = float(item["confidence"])
                except (TypeError, ValueError):
                    pass
        per_run.append(run)
    if not per_run:
        return None
    common = set.intersection(*(set(r) for r in per_run)) if per_run else set()
    if not common:
        return None
    return max(max(r[i] for r in per_run) - min(r[i] for r in per_run) for i in common)


@dataclass
class Stability:
    runs: int
    parseable: int
    text_identical_rate: float
    decision_identical_rate: float
    max_confidence_spread: float | None
    label: str
    reasons: list[str] = field(default_factory=list)


def assess(replies: list[str], attempted: int | None = None) -> Stability:
    """`replies` are the successful replies; `attempted` is how many calls
    were tried (failed calls make the sample smaller, and say so)."""
    n = len(replies)
    attempted = attempted if attempted is not None else n
    if n == 0:
        return Stability(attempted, 0, 0.0, 0.0, None, "no data", ["every call failed"])
    text_rate = Counter(replies).most_common(1)[0][1] / n
    sigs = [decision_signature(r) for r in replies]
    parseable = sum(s is not None for s in sigs)
    if parseable == 0:
        return Stability(attempted, 0, text_rate, 0.0, None, "unparseable", ["no reply was valid JSON of a known shape"])
    parsed_sigs = [s for s in sigs if s is not None]
    dec_rate = Counter(parsed_sigs).most_common(1)[0][1] / len(parsed_sigs)
    spread = confidence_spread([r for r, s in zip(replies, sigs) if s is not None])
    reasons = []
    if n < attempted:
        reasons.append(f"{attempted - n} of {attempted} calls failed, so the sample is smaller")
    if parseable < n:
        reasons.append(f"{n - parseable} of {n} replies were not valid JSON of the expected shape")
    if attempted < MIN_RUNS_FOR_LABEL or n < MIN_RUNS_FOR_LABEL:
        label = "insufficient samples"
        reasons.append(f"need at least {MIN_RUNS_FOR_LABEL} successful runs to label")
    elif parseable < n or dec_rate < 1.0:
        label = "unstable decisions"
        if dec_rate < 1.0:
            reasons.append(f"only {dec_rate:.0%} of runs agreed on what to flag")
    elif text_rate == 1.0:
        label = "identical"
    else:
        label = "decisions stable, wording varies"
        reasons.append(f"{text_rate:.0%} of runs had the most common exact text")
    return Stability(attempted, parseable, text_rate, dec_rate, spread, label, reasons)

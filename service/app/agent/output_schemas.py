"""Strict JSON-schema output for the LLM replies this project parses.

OFF by default. Set LLM_STRUCTURED_OUTPUT=1 to ask the provider to
constrain its reply to one of these schemas (Groq: response_format
json_schema with strict=true, documented for openai/gpt-oss-20b;
Gemini: response_mime_type + response_json_schema).

What it buys: the reply is valid JSON of the right SHAPE, so fewer
"unparseable" calls. What it does not buy: correct judgments, or
byte-identical text across calls. A schema-valid reply can still say
the wrong thing, so every consumer still validates values.

Which schema applies is decided from the system prompt's own declared
reply shape, so callers and prompts are unchanged. A prompt that matches
none (for example the instruction compiler, whose reply is a union of two
shapes) is sent exactly as before.

Strict-mode rules (Groq): every property listed in `required`, and
`additionalProperties: false` on every object. These schemas follow them,
which also keeps them inside Gemini's supported JSON-Schema subset.
"""
from __future__ import annotations

import os

_NUM = {"type": "number"}
_INT = {"type": "integer"}
_STR = {"type": "string"}
_LABEL = {"type": "string", "enum": ["confirmed", "needs_review", "rejected"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _wrap(key: str, item: dict) -> dict:
    return _obj({key: {"type": "array", "items": item}})


SCHEMAS: dict[str, dict] = {
    "row_flags": _wrap("flags", _obj({"row_index": _INT, "reason": _STR, "confidence": _NUM})),
    "row_verifications": _wrap("verifications", _obj({"row_index": _INT, "label": _LABEL, "notes": _STR})),
    "value_anomalies": _wrap("anomalies", _obj({"value": _STR, "reason": _STR, "confidence": _NUM})),
    "pair_flags": _wrap(
        "flags", _obj({"from_row_index": _INT, "to_row_index": _INT, "reason": _STR, "confidence": _NUM})
    ),
    "pair_verifications": _wrap(
        "verifications",
        _obj({"from_row_index": _INT, "to_row_index": _INT, "label": _LABEL, "notes": _STR}),
    ),
}

# The reply-shape line each prompt declares, mapped to its schema.
_ANCHORS = (
    ('{"flags": [{"from_row_index"', "pair_flags"),
    ('{"verifications": [{"from_row_index"', "pair_verifications"),
    ('{"flags": [{"row_index"', "row_flags"),
    ('{"verifications": [{"row_index"', "row_verifications"),
    ('{"anomalies": [{"value"', "value_anomalies"),
)


def enabled() -> bool:
    return os.environ.get("LLM_STRUCTURED_OUTPUT", "").strip().lower() in {"1", "true", "yes", "on"}


def schema_name_for(system_prompt: str) -> str | None:
    """Name of the schema for this prompt, or None when it should be sent
    unconstrained (feature off, or the prompt's reply shape is not one we
    have a schema for)."""
    if not enabled():
        return None
    for anchor, name in _ANCHORS:
        if anchor in system_prompt:
            return name
    return None

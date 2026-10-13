"""JSON schemas and schema helpers for the Trix Arena plugin.

One input schema per tool: ``PING_SCHEMA`` (stage 0) plus the five
stage-2 tool schemas (plan / start / status / cancel / result).
``TOOL_SCHEMAS`` maps the tool name to its schema; ``plugin.yaml``
references the very same objects via ``"<module>.<ATTRIBUTE>"`` strings
(for example ``"schemas.PLAN_SCHEMA"``) — that string-reference contract
is resolved by the plugin host at load time, and every referenced
attribute is guaranteed to be listed in ``TOOL_SCHEMAS`` too.

Validation is structural: object type, no unknown properties, and the
declared scalar type of every supplied key (``minimum``/``maximum`` are
NOT checked here). Semantic range checks (for example
``1 <= agents <= combo_count``) live in the tool layer and are reported
as ``ArenaError`` messages so all domain errors speak one language.

The orchestrator payload schemas (solve / attack / defense / judge) are
reserved for stage 3 and will live next to these.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Tool input schemas (JSON Schema draft-07 style, subset)
# ---------------------------------------------------------------------------

PING_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_ping input",
    "type": "object",
    "properties": {
        "payload": {
            "type": ["string", "null"],
            "description": "Optional echo payload; ignored by the tool.",
        }
    },
    "additionalProperties": False,
}

PLAN_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_plan input",
    "type": "object",
    "properties": {
        "agents": {
            "type": "integer",
            "minimum": 1,
            "description": "Number of tournament participants (default: 100).",
        },
        "wave": {
            "type": "integer",
            "minimum": 1,
            "description": "Sub-agent calls per wave (default: 10).",
        },
    },
    "additionalProperties": False,
}

START_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_start input",
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task every participant must solve.",
        },
        "agents": {
            "type": "integer",
            "minimum": 1,
            "description": "Participant count (default: 100).",
        },
        "seed": {
            "type": "string",
            "description": "Deterministic deal seed (default: 'arena').",
        },
        "wave": {
            "type": "integer",
            "minimum": 1,
            "description": "Sub-agent calls per wave (default: 10).",
        },
        "dir": {
            "type": "string",
            "description": (
                "Base directory for the runs; relative paths stay relative "
                "to the working directory (default: '.arena')."
            ),
        },
    },
    "additionalProperties": False,
}

STATUS_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_status input",
    "type": "object",
    "properties": {
        "run": {
            "type": "string",
            "description": "Run id, or 'LATEST' to use the last started run (default).",
        },
        "dir": {
            "type": "string",
            "description": "Base directory containing the runs (default: '.arena').",
        },
    },
    "additionalProperties": False,
}

CANCEL_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_cancel input",
    "type": "object",
    "properties": {
        "run": {
            "type": "string",
            "description": "Run id, or 'LATEST' to use the last started run (default).",
        },
        "dir": {
            "type": "string",
            "description": "Base directory containing the runs (default: '.arena').",
        },
        "purge": {
            "type": "boolean",
            "description": "Also delete the run directory (default: false).",
        },
    },
    "additionalProperties": False,
}

RESULT_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "arena_result input",
    "type": "object",
    "properties": {
        "run": {
            "type": "string",
            "description": "Run id, or 'LATEST' to use the last started run (default).",
        },
        "dir": {
            "type": "string",
            "description": "Base directory containing the runs (default: '.arena').",
        },
        "collect": {
            "type": "boolean",
            "description": (
                "Ingest judge verdicts present on disk before reporting "
                "(default: true)."
            ),
        },
    },
    "additionalProperties": False,
}

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "arena_ping": PING_SCHEMA,
    "arena_plan": PLAN_SCHEMA,
    "arena_start": START_SCHEMA,
    "arena_status": STATUS_SCHEMA,
    "arena_cancel": CANCEL_SCHEMA,
    "arena_result": RESULT_SCHEMA,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def schema_error(tool_name: str, data: Any) -> Optional[str]:
    """Return the first structural violation of ``tool_name``'s input
    schema as a human-readable string, or ``None`` when ``data`` validates.

    Raises ``KeyError`` for an unknown ``tool_name`` (a host configuration
    bug, not a user input problem).
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        raise KeyError("unknown tool: %r" % (tool_name,))
    if schema.get("type") != "object":
        return None
    if not isinstance(data, dict):
        return "%s expects a JSON object, got %s" % (tool_name, type(data).__name__)
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        allowed = ", ".join(sorted(properties)) or "none"
        for key in sorted(data):
            if key not in properties:
                return "%s got unknown input key %r (allowed: %s)" % (tool_name, key, allowed)
    for key in sorted(data):
        spec = properties.get(key)
        if not spec:
            continue
        expected = spec.get("type")
        if expected is None or _matches_type(data[key], expected):
            continue
        expected_txt = expected if isinstance(expected, str) else " or ".join(expected)
        return "%s: input %r must be of type %s (got %s)" % (
            tool_name, key, expected_txt, type(data[key]).__name__,
        )
    return None


def _matches_type(value: Any, expected: Any) -> bool:
    """Check ``value`` against a JSON-schema ``type`` (string or list)."""
    if isinstance(expected, str):
        expected = (expected,)
    for name in expected:
        if name == "string" and isinstance(value, str):
            return True
        if name == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if name == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if name == "boolean" and isinstance(value, bool):
            return True
        if name == "null" and value is None:
            return True
        if name == "object" and isinstance(value, dict):
            return True
        if name == "array" and isinstance(value, list):
            return True
    return False


def validate_tool_input(tool_name: str, data: Any) -> bool:
    """Validate ``data`` against the input schema of ``tool_name``.

    Boolean convenience wrapper around :func:`schema_error`, which carries
    the human-readable message. Structural checks only (object type, no
    unknown properties, declared scalar types); full JSON Schema
    validation would need a real validator dependency and is out of scope.
    """
    return schema_error(tool_name, data) is None


def loads(text: str) -> Any:
    """Parse a JSON document, raising ``ValueError`` on trailing garbage.

    ``json.loads`` already rejects trailing non-whitespace data; this wrapper
    exists so every plugin entry point reports malformed JSON through the
    same error path.
    """
    return json.loads(text)

"""JSON schemas and schema helpers for the Trix Arena plugin.

Stage 0 keeps these minimal: one schema per tool and a small ``loads``
helper that future stages will use to validate orchestrator payloads
(solve/attack/defense/judge messages) in one place.
"""

from __future__ import annotations

import json
from typing import Any, Dict

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

# Reserved for stage 2/3: arena_plan, arena_start, arena_status,
# arena_cancel, arena_result and the orchestrator job schemas
# (solve / attack / defense / judge) will live next to these.

TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "arena_ping": PING_SCHEMA,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def validate_tool_input(tool_name: str, data: Any) -> bool:
    """Validate ``data`` against the input schema of ``tool_name``.

    Stage 0 performs only structural checks (object type, no unknown
    properties). Full JSON Schema validation lands with the orchestrator
    stage together with a real validator dependency.
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        raise KeyError("unknown tool: %r" % (tool_name,))
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            return False
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in props:
                    return False
    return True


def loads(text: str) -> Any:
    """Parse a JSON document, raising ``ValueError`` on trailing garbage.

    ``json.loads`` already rejects trailing non-whitespace data; this wrapper
    exists so every plugin entry point reports malformed JSON through the
    same error path.
    """
    return json.loads(text)

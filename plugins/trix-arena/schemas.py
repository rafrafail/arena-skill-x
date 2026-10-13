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

The orchestrator payload schemas (solve / attack / defense / judge)
live next to these: ``SOLVE_SCHEMA`` / ``ATTACK_SCHEMA`` /
``DEFENSE_SCHEMA`` / ``JUDGE_SCHEMA``, keyed by core job kind in
``JOB_SCHEMAS`` and enforced by ``payload_error`` (stage 3).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Orchestrator payload schemas (stage 3)
#
# The orchestrator (arena/orchestrator.py) hands each job to a sub-agent
# backend and receives back a *payload* dict: the result content of that
# job. These schemas validate payloads structurally. Unlike the tool
# schemas above they also enforce the ``required`` list — a job result
# without its content fields is invalid, not merely incomplete — so the
# orchestrator never writes an unusable output file and can retry the job.
# ---------------------------------------------------------------------------

_KEY_PROP: Dict[str, Any] = {
    "type": "string",
    "description": (
        "Idempotency key of the job as printed in the prompt. The backend "
        "may echo it back; a mismatch makes the payload invalid."
    ),
}


def _payload_schema(
    title: str, required: Tuple[str, ...], properties: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """Build a payload schema: object type, required keys, no extras."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "object",
        "required": list(required),
        "properties": dict(properties, idempotency_key=_KEY_PROP),
        "additionalProperties": False,
    }


SOLVE_SCHEMA = _payload_schema(
    "arena solve (competitor spawn) payload",
    ("solution",),
    {
        "solution": {
            "type": "string",
            "description": "The competitor's complete answer to the task.",
        },
    },
)

ATTACK_SCHEMA = _payload_schema(
    "arena attack (attacker) payload",
    ("attack",),
    {
        "attack": {
            "type": "string",
            "description": "The concrete weaknesses found in the opponent's solution.",
        },
    },
)

DEFENSE_SCHEMA = _payload_schema(
    "arena defense (defender) payload",
    ("defense", "revised_solution"),
    {
        "defense": {
            "type": "string",
            "description": "The rebuttal to the attacks on this solution.",
        },
        "revised_solution": {
            "type": "string",
            "description": "The improved complete solution after the attacks.",
        },
    },
)

JUDGE_SCHEMA = _payload_schema(
    "arena judge verdict payload",
    ("winner", "scores"),
    {
        "winner": {
            "type": "string",
            "description": "Id of the winning solution: the match's a/b ids, or x/y for the final.",
        },
        "scores": {
            "type": "object",
            "description": (
                "Per-solution rubric scores keyed by solution id, each an "
                "object of 0-10 numbers on the rubric axes."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Why the winning solution wins.",
        },
        "survived": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Attacks that neither solution survived.",
        },
        "standing": {
            "type": "object",
            "description": "Open challenges per solution id (list of strings).",
        },
        "fixed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Fixes the winner should still apply (final only).",
        },
    },
)

#: Payload schema for every core job kind (``core.phase_jobs`` ``kind``s).
#: ``judge`` and ``final`` share the verdict schema; the final's winner is
#: named ``x``/``y`` exactly as in the upstream ``collect``/``decide`` flow.
JOB_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "competitor": SOLVE_SCHEMA,
    "attacker": ATTACK_SCHEMA,
    "defender": DEFENSE_SCHEMA,
    "judge": JUDGE_SCHEMA,
    "final": JUDGE_SCHEMA,
}

#: Human-readable payload kind names (stage-3 checklist vocabulary).
PAYLOAD_KINDS: Tuple[str, ...] = ("solve", "attack", "defense", "judge")


def payload_error(kind: str, data: Any) -> Optional[str]:
    """Return the first structural violation of ``kind``'s payload schema
    as a human-readable string, or ``None`` when ``data`` validates.

    Unlike :func:`schema_error` this also enforces the schema's
    ``required`` list: a job payload without its content fields is a
    schema error (retryable), never written to disk.

    Raises ``KeyError`` for an unknown ``kind`` (orchestrator wiring bug,
    not a backend problem).
    """
    schema = JOB_SCHEMAS.get(kind)
    if schema is None:
        raise KeyError("unknown job kind: %r" % (kind,))
    if not isinstance(data, dict):
        return "%s payload must be a JSON object, got %s" % (kind, type(data).__name__)
    for key in schema["required"]:
        if key not in data:
            return "%s payload is missing required key %r (got: %s)" % (
                kind, key, ", ".join(sorted(data)) or "none",
            )
    properties = schema["properties"]
    allowed = ", ".join(sorted(properties))
    for key in sorted(data):
        if key not in properties:
            return "%s payload got unknown input key %r (allowed: %s)" % (kind, key, allowed)
    for key in sorted(data):
        spec = properties[key]
        if not _matches_type(data[key], spec.get("type")):
            return "%s: payload key %r must be of type %s (got %s)" % (
                kind, key, spec.get("type"), type(data[key]).__name__,
            )
    return None


def validate_payload(kind: str, data: Any) -> bool:
    """Validate ``data`` against the payload schema of job ``kind``.

    Boolean convenience wrapper around :func:`payload_error`.
    """
    return payload_error(kind, data) is None

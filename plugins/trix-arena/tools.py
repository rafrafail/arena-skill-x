"""Tool handlers for the Trix Arena plugin.

The plugin context registers tools listed in ``plugin.yaml``. Stage 0
provides ``arena_ping`` only; the tournament tools (arena_plan,
arena_start, arena_status, arena_cancel, arena_result) arrive with the
plugin-API and orchestrator stages.
"""

from __future__ import annotations

from typing import Any, Dict

from . import schemas


def arena_ping(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_ping`` tool.

    ``data`` may be a dict matching ``PING_SCHEMA`` (an optional
    ``payload`` key) or ``None``. Always succeeds with ``{"pong": True}``;
    schema violations return a structured error instead of raising, so the
    plugin host never sees an unhandled exception from a health check.
    """
    if data is None:
        data = {}
    if not schemas.validate_tool_input("arena_ping", data):
        return {"pong": False, "error": "input does not match arena_ping schema"}
    payload = data.get("payload")
    result: Dict[str, Any] = {"pong": True, "plugin": "trix-arena"}
    if payload is not None:
        result["payload"] = payload
    return result

"""Command handlers for the Trix Arena plugin.

Stage 0 exposes a single ``/arena ping`` command answering ``pong``.
Later stages add plan/start/status/result/cancel subcommands on top of the
shared tournament core in ``plugins/trix_arena.arena``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PONG = "pong"

# Subcommands registered for /arena. Stage 0 implements only "ping";
# the others are listed so argument parsing fails with a clear message
# instead of a silent fallthrough.
KNOWN_SUBCOMMANDS = ("ping", "plan", "start", "status", "result", "cancel")


def arena_command(args: Optional[List[str]] = None, **kwargs: Any) -> str:
    """Handle the ``/arena`` command.

    Usage (stage 0)::

        /arena ping

    Any other subcommand, or no subcommand, produces a helpful message
    naming the known subcommands.
    """
    parts = list(args or [])
    if not parts:
        return _usage()
    sub = parts[0].lower()
    if sub == "ping":
        return _handle_ping(parts[1:])
    if sub in KNOWN_SUBCOMMANDS:
        return (
            "arena: subcommand %r is not implemented yet "
            "(planned for a later stage); try 'arena ping'." % (sub,)
        )
    return _usage()


def arena_ping(payload: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    """Tool handler: always answers ``pong``.

    Returns a structured result so the tool layer can render or log it
    without parsing prose.
    """
    result: Dict[str, Any] = {"pong": True, "plugin": "trix-arena"}
    if payload is not None:
        result["payload"] = payload
    return result


def _handle_ping(rest: List[str]) -> str:
    # "arena ping" must answer exactly "pong", no matter what follows.
    return PONG


def _usage() -> str:
    return (
        "arena: unknown or missing subcommand. Known: "
        + ", ".join(KNOWN_SUBCOMMANDS)
        + ". Stage 0 implements only: ping."
    )

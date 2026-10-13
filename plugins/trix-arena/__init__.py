"""Trix Arena plugin package.

User-facing name: Trix Arena. Technical id: ``trix-arena``.

This package is a plugin for Trix Agent. It exposes the ``/arena`` command
and a set of tools wrapping a deterministic tournament engine (see the
``arena`` subpackage) in which N independent LLM participants each get the
same task with a different strategy card and then attack, defend and judge
each other in a single-elimination bracket until one solution survives.

Upstream origin: Jakeschincariol/arena-skill (MIT). The upstream skill lives
in ``skills/arena/`` at the repository root and is never modified here.
"""

__version__ = "0.1.0"

PLUGIN_ID = "trix-arena"
PLUGIN_NAME = "Trix Arena"


def register(context) -> None:
    """Register the plugin with the Trix Agent plugin context.

    Wires the ``/arena`` command to
    :func:`plugins.trix_arena.commands.arena_command` and registers the
    six plugin tools: the ``arena_ping`` health check (stage 0) and the
    five tournament tools of stage 2 — ``arena_plan`` (cost preview),
    ``arena_start`` (create a run), ``arena_status`` (inspect a run),
    ``arena_cancel`` (cancel/purge a run) and ``arena_result``
    (collect judge verdicts).
    """
    from . import commands, tools

    context.register_command("arena", commands.arena_command)
    context.register_tool("arena_ping", tools.arena_ping)
    context.register_tool("arena_plan", tools.arena_plan)
    context.register_tool("arena_start", tools.arena_start)
    context.register_tool("arena_status", tools.arena_status)
    context.register_tool("arena_cancel", tools.arena_cancel)
    context.register_tool("arena_result", tools.arena_result)


def _compat_register(context) -> None:
    """Back-compat alias used by older plugin loaders that look up
    ``register_plugin`` instead of ``register``."""
    register(context)

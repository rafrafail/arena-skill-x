"""Tool handlers for the Trix Arena plugin.

Stage 2 exposes the plugin API surface over the tournament core
(``arena/core.py``):

- ``arena_ping``    health check, always ``{"pong": True}`` (stage 0)
- ``arena_plan``    cost preview: rounds / LLM calls / waves; pure, no state
- ``arena_start``   create a run: deal strategy cards, open round 1
- ``arena_status``  inspect a run: alive agents, open round, pending jobs
- ``arena_result``  collect judge verdicts (``core.collect``) and report
- ``arena_cancel``  cancel a run, optionally purge its directory

Calling convention: every tool returns a dict with an ``ok`` key —
``{"ok": True, ...}`` on success, ``{"ok": False, "error": "..."}`` on any
schema, domain (``ArenaError``) or filesystem failure. Tools never raise
toward the plugin host; the one exception is ``KeyError`` for an unknown
tool name, raised by the schema lookup itself (host misconfiguration,
not user input).

State contract: runs live under a caller-provided base directory
(default ``.arena``, relative to the agent's working directory; relative
paths stay relative). The plugin writes only inside the run directory
plus the ``<base>/LATEST`` pointer file, and never stores absolute paths
in state.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import schemas
from .arena import core


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _base_dir(body: Dict[str, Any]) -> str:
    """Arena base directory for the run (default: ``core.ROOT``)."""
    base = body.get("dir") or core.ROOT
    if not isinstance(base, str) or not base.strip():
        raise core.ArenaError("dir must be a non-empty string (got %r)" % (base,))
    return base


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise core.ArenaError("%s must be an integer (got %r)" % (name, value))
    return value


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise core.ArenaError("%s must be a non-empty string (got %r)" % (name, value))
    return value


def _require_wave(wave: int) -> None:
    if wave < 1:
        raise core.ArenaError("wave must be at least 1 (got %r)" % (wave,))


def _check_agents(agents: int, cap: int) -> None:
    if not 1 <= agents <= cap:
        raise core.ArenaError(
            "agents must be between 1 and %d (got %r); use arena_plan to preview costs" % (cap, agents)
        )


def _set_latest(base: str, run_id: str) -> None:
    """Point ``<base>/LATEST`` at ``run_id`` (atomic replace)."""
    os.makedirs(base, exist_ok=True)
    pointer = os.path.join(base, core.LATEST)
    tmp = pointer + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(run_id + "\n")
    os.replace(tmp, pointer)


def _clear_latest(base: str, run_name: str) -> None:
    """Drop the LATEST pointer if it still references ``run_name``."""
    pointer = os.path.join(base, core.LATEST)
    if os.path.isfile(pointer):
        with open(pointer, encoding="utf-8") as fh:
            if fh.read().strip() == run_name:
                os.remove(pointer)


def _read_latest(base: str) -> str:
    pointer = os.path.join(base, core.LATEST)
    if not os.path.isfile(pointer):
        raise core.ArenaError(
            "no %s pointer in %s (no arena run started? use arena_start first)"
            % (core.LATEST, base)
        )
    with open(pointer, encoding="utf-8") as fh:
        run = fh.read().strip()
    if not run:
        raise core.ArenaError("%s pointer in %s is empty" % (core.LATEST, base))
    return run


def _ensure_within(base: str, run_dir: str) -> None:
    """Reject run ids that would escape the arena base directory."""
    rel = os.path.relpath(os.path.abspath(run_dir), os.path.abspath(base))
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise core.ArenaError("run %r escapes the arena directory %r" % (run_dir, base))


def _resolve_run_dir(base: str, run: Optional[str]) -> str:
    """Resolve the ``run`` argument (default ``LATEST``) to a run directory."""
    run = run or core.LATEST
    _require_str(run, "run")
    if run == core.LATEST:
        run = _read_latest(base)
    run_dir = os.path.join(base, run)
    _ensure_within(base, run_dir)
    if not os.path.isdir(run_dir):
        raise core.ArenaError("no arena run directory %r under %s" % (run, base))
    return run_dir


def _load_run(base: str, run: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """Resolve + load a run state, migrating older schema versions in place.

    ``base_dir='.'`` resolves the stored relative run directory against the
    working directory, mirroring how :func:`_start_impl` stored it — no
    absolute paths are introduced.
    """
    run_dir = _resolve_run_dir(base, run)
    state = core.load_state(run_dir, base_dir=".")
    core.ensure_current_schema(state, base_dir=".")
    return run_dir, state


def _run_name(base: str, run_dir: str) -> str:
    """Run id as a path relative to ``base`` (e.g. ``arena-x-20260101-120000``)."""
    return os.path.relpath(run_dir, base)


def _new_run_id(base: str, seed: str) -> str:
    """Free run id ``<slug>-<timestamp>`` (``-2``, ``-3``, ... on collisions)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(seed).lower()).strip("-")[:24] or "run"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = "%s-%s" % (slug, stamp)
    counter = 2
    while os.path.exists(os.path.join(base, candidate)):
        candidate = "%s-%s-%d" % (slug, stamp, counter)
        counter += 1
        if counter > 100:
            raise core.ArenaError("could not find a free run id under %s" % base)
    return candidate


def _finished(state: Dict[str, Any]) -> bool:
    """A run is finished once a champion exists and the optional final was judged."""
    final = state.get("final")
    return bool(state.get("champion")) and (final is None or bool(final.get("result")))


def _guarded(name: str, data: Any, impl: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    """Validate input, run ``impl``, and convert failures to ``{"ok": False}``.

    Success results get ``ok: True`` added. Never raises except the
    ``KeyError`` from the schema lookup itself (unknown tool name).
    """
    try:
        if data is None:
            data = {}
        message = schemas.schema_error(name, data)
        if message:
            return {"ok": False, "error": message}
        result = impl(data)
        result["ok"] = True
        return result
    except core.ArenaError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": "filesystem error: %s" % exc}


# ---------------------------------------------------------------------------
# Tool handlers (public API)
# ---------------------------------------------------------------------------


def arena_ping(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_ping`` tool.

    ``data`` may be a dict matching ``PING_SCHEMA`` (an optional
    ``payload`` key) or ``None``. Always succeeds with ``{"pong": True}``;
    schema violations return a structured error instead of raising, so the
    plugin host never sees an unhandled exception from a health check.
    """
    if data is None:
        data = {}
    message = schemas.schema_error("arena_ping", data)
    if message:
        return {"pong": False, "error": message}
    payload = data.get("payload")
    result: Dict[str, Any] = {"pong": True, "plugin": "trix-arena"}
    if payload is not None:
        result["payload"] = payload
    return result


def arena_plan(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_plan`` tool: pure cost preview, no state touched."""
    return _guarded("arena_plan", data, _plan_impl)


def arena_start(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_start`` tool: create a run and open round 1."""
    return _guarded("arena_start", data, _start_impl)


def arena_status(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_status`` tool: inspect a run without side effects."""
    return _guarded("arena_status", data, _status_impl)


def arena_cancel(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_cancel`` tool: cancel a run, optionally purge it."""
    return _guarded("arena_cancel", data, _cancel_impl)


def arena_result(data: Any = None, **kwargs: Any) -> Dict[str, Any]:
    """Handle the ``arena_result`` tool: collect verdicts and report."""
    return _guarded("arena_result", data, _result_impl)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _plan_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    agents = body.get("agents", core.DEFAULT_AGENTS)
    wave = body.get("wave", core.DEFAULT_WAVE)
    _require_int(agents, "agents")
    _require_int(wave, "wave")
    _require_wave(wave)
    cap = core.combo_count(core.load_strategies())
    _check_agents(agents, cap)
    return {"plan": core.plan_totals(agents, wave)}


def _start_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    agents = body.get("agents", core.DEFAULT_AGENTS)
    wave = body.get("wave", core.DEFAULT_WAVE)
    seed = body.get("seed", "arena")
    task = body.get("task")
    base = _base_dir(body)

    _require_int(agents, "agents")
    _require_int(wave, "wave")
    _require_wave(wave)
    _require_str(seed, "seed")
    if task is not None and not isinstance(task, str):
        raise core.ArenaError("task must be a string (got %r)" % (task,))
    if not task or not task.strip():
        raise core.ArenaError("start needs a task (pass task: '<what to compete on>')")

    strategies = core.load_strategies()
    cap = core.combo_count(strategies)
    _check_agents(agents, cap)

    run_id = _new_run_id(base, seed)
    run_dir = os.path.join(base, run_id)
    os.makedirs(run_dir, exist_ok=True)

    state = core.new_state(agents, seed, strategies, run_dir, wave=wave)
    if task:
        state["task"] = task
    core.save_state(state)
    _set_latest(base, run_id)

    spawn = core.phase_jobs(state, "spawn")
    return {
        "run": run_id,
        "dir": run_dir,
        "state_file": os.path.join(run_dir, core.STATE_FILE),
        "agents": agents,
        "seed": seed,
        "wave": wave,
        "cards_available": cap,
        "spawn_jobs": len(spawn),
        "task": task,
    }


def _status_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    base = _base_dir(body)
    run_dir, state = _load_run(base, body.get("run"))

    jobs: Dict[str, Dict[str, int]] = {}
    for phase in core.PHASES:
        pending = core.phase_jobs(state, phase)
        jobs[phase] = {"jobs": len(pending), "missing": len(core.missing_jobs(pending))}

    rd = core.current_round(state)
    unrecorded = [m["id"] for m in rd["matches"] if not m["winner"]] if rd else []
    sizes = core.bracket_sizes(int(state.get("agents_n") or len(state["agents"])))

    return {
        "run": _run_name(base, run_dir),
        "dir": run_dir,
        "seed": state.get("seed"),
        "wave": state.get("wave"),
        "task": state.get("task"),
        "agents_n": state.get("agents_n"),
        "alive": len(core.alive_ids(state)),
        "rounds_total": len(sizes) - 1,
        "rounds_played": core.rounds_played(state),
        "current_round": rd["n"] if rd else None,
        "unrecorded": unrecorded,
        "champion": state.get("champion"),
        "final": state.get("final"),
        "cancelled": bool(state.get("cancelled")),
        "finished": _finished(state),
        "jobs": jobs,
        "state_schema_version": state.get("state_schema_version"),
    }


def _cancel_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    base = _base_dir(body)
    run_dir, state = _load_run(base, body.get("run"))
    run_name = _run_name(base, run_dir)
    purge = bool(body.get("purge", False))

    if purge:
        shutil.rmtree(run_dir)
        _clear_latest(base, run_name)
        return {"cancelled": True, "run": run_name, "purged": True}

    if state.get("cancelled"):
        return {"cancelled": True, "run": run_name, "purged": False, "already": True}
    state["cancelled"] = True
    core.save_state(state)
    return {"cancelled": True, "run": run_name, "purged": False}


def _result_impl(body: Dict[str, Any]) -> Dict[str, Any]:
    base = _base_dir(body)
    run_dir, state = _load_run(base, body.get("run"))

    collected: List[Dict[str, str]] = []
    if body.get("collect", True):
        for match_id, status, note in core.collect(state):
            collected.append({"match": match_id, "status": status, "note": note})
        if collected:
            core.save_state(state)

    return {
        "run": _run_name(base, run_dir),
        "champion": state.get("champion"),
        "final": state.get("final"),
        "collected": collected,
        "alive": len(core.alive_ids(state)),
        "rounds_played": core.rounds_played(state),
        "finished": _finished(state),
        "cancelled": bool(state.get("cancelled")),
    }

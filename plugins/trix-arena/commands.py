"""Command handlers for the Trix Arena plugin.

``/arena`` exposes six subcommands on top of the stage-2 tools:

    /arena ping                                   answer exactly "pong"
    /arena plan [--agents N] [--wave N]           cost preview (no state)
    /arena start <task> [--agents N] [--seed S] [--wave N] [--dir PATH]
                                                  create a run, deal cards
    /arena status [--run ID] [--dir PATH]         inspect the open round
    /arena result [--run ID] [--dir PATH]         collect verdicts, report
    /arena cancel [--run ID] [--dir PATH] [--purge true|false]
                                                  cancel (optionally purge)

Parsing is strict: unknown options, single-dash typos, missing values and
wrong value types all fail with a one-line explanation instead of being
guessed at. Domain failures surface as ``"arena <sub>: <message>"`` text;
tool failures are never allowed to raise through the command layer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import tools
from .arena.core import ArenaError

PONG = "pong"

# Subcommands registered for /arena (all implemented since stage 2).
KNOWN_SUBCOMMANDS = ("ping", "plan", "start", "status", "result", "cancel")

_TYPE_NAMES = {int: "an integer", str: "a string"}


def arena_command(args: Optional[List[str]] = None, **kwargs: Any) -> str:
    """Handle the ``/arena`` command.

    ``args`` are the words after ``/arena``. Unknown or missing
    subcommands produce a usage message naming the known subcommands;
    ``ArenaError`` from strict parsing is reported as
    ``"arena <sub>: <message>"``.
    """
    parts = list(args or [])
    if not parts:
        return _usage()
    sub = parts[0].lower()
    handler = _HANDLERS.get(sub)
    if handler is None:
        return _usage()
    try:
        return handler(parts[1:])
    except ArenaError as exc:
        return "arena %s: %s" % (sub, exc)


# ---------------------------------------------------------------------------
# Strict argument parsing
# ---------------------------------------------------------------------------


def _as_bool(option: str, raw: str) -> bool:
    low = raw.strip().lower()
    if low in ("true", "yes", "1"):
        return True
    if low in ("false", "no", "0"):
        return False
    raise ArenaError("option %s expects true|false, got %r" % (option, raw))


def _parse_flags(
    rest: List[str], allowed: Dict[str, Any]
) -> Tuple[List[str], Dict[str, Any]]:
    """Split ``--name value`` flags from positional words; strict on errors.

    ``allowed`` maps flag name to its value type (``int``, ``str`` or
    ``bool``). Anything else — unknown option, single-dash typo, missing
    value, unparseable value — raises ``ArenaError`` with a one-line
    explanation.
    """
    positionals: List[str] = []
    flags: Dict[str, Any] = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            name = tok[2:]
            if name not in allowed:
                raise ArenaError(
                    "unknown option %r (allowed: %s)"
                    % (tok, ", ".join("--" + k for k in sorted(allowed)))
                )
            if i + 1 >= len(rest):
                raise ArenaError("option %s needs a value" % tok)
            raw = rest[i + 1]
            typ = allowed[name]
            if typ is bool:
                flags[name] = _as_bool(tok, raw)
            else:
                try:
                    flags[name] = typ(raw)
                except ValueError:
                    raise ArenaError(
                        "option %s expects %s, got %r" % (tok, _TYPE_NAMES[typ], raw)
                    )
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            raise ArenaError(
                "unknown option %r (options use the '--name value' form)" % tok
            )
        positionals.append(tok)
        i += 1
    return positionals, flags


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _handle_ping(rest: List[str]) -> str:
    # "arena ping" must answer exactly "pong", no matter what follows.
    return PONG


def _handle_plan(rest: List[str]) -> str:
    _positionals, flags = _parse_flags(rest, {"agents": int, "wave": int})
    body = {k: flags[k] for k in ("agents", "wave") if k in flags}
    out = tools.arena_plan(body)
    if not out.get("ok"):
        return "arena plan: %s" % out.get("error")
    plan = out["plan"]
    lines = [
        "arena plan: %d participants, %d rounds, %d LLM calls in %d waves"
        % (plan["agents"], plan["rounds"], plan["calls"], plan["waves"]),
        "round  alive  matches  bye  calls  waves",
    ]
    for row in plan["rows"]:
        lines.append(
            "%5d %6d %8d %4d %6d %6d"
            % (row["round"], row["alive"], row["matches"], row["bye"], row["calls"], row["waves"])
        )
    return "\n".join(lines)


def _handle_start(rest: List[str]) -> str:
    positionals, flags = _parse_flags(
        rest, {"agents": int, "seed": str, "wave": int, "dir": str}
    )
    if not positionals:
        raise ArenaError(
            'start needs a task: arena start "<task>" '
            "[--agents N] [--seed S] [--wave N] [--dir PATH]"
        )
    body: Dict[str, Any] = {"task": " ".join(positionals)}
    body.update(flags)
    out = tools.arena_start(body)
    if not out.get("ok"):
        return "arena start: %s" % out.get("error")
    return (
        "arena start: run %(run)s created under %(dir)s "
        "(%(agents)d participants, seed '%(seed)s').\n"
        "%(spawn_jobs)d spawn jobs pending; follow with '/arena status' "
        "or the arena_status tool." % out
    )


def _handle_status(rest: List[str]) -> str:
    _positionals, flags = _parse_flags(rest, {"run": str, "dir": str})
    out = tools.arena_status(flags)
    if not out.get("ok"):
        return "arena status: %s" % out.get("error")

    jobs = out["jobs"]
    jobs_txt = ", ".join(
        "%s %d/%d missing" % (phase, jobs[phase]["missing"], jobs[phase]["jobs"])
        for phase in jobs
    )
    lines = [
        "arena status: run %s (dir %s)" % (out["run"], out["dir"]),
        "task: %s" % (out["task"] or "n/a"),
        "alive %d/%d, rounds played %d/%d, current round %s, champion: %s, cancelled: %s"
        % (
            out["alive"], out["agents_n"], out["rounds_played"], out["rounds_total"],
            out["current_round"] if out["current_round"] else "none",
            out["champion"] or "none", "yes" if out["cancelled"] else "no",
        ),
        "jobs (missing/total): %s" % jobs_txt,
    ]
    if out["unrecorded"]:
        lines.append("unrecorded matches: %s" % ", ".join(out["unrecorded"]))
    if out["finished"]:
        lines.append("finished: yes")
    return "\n".join(lines)


def _handle_result(rest: List[str]) -> str:
    _positionals, flags = _parse_flags(rest, {"run": str, "dir": str})
    out = tools.arena_result(flags)
    if not out.get("ok"):
        return "arena result: %s" % out.get("error")

    lines = [
        "arena result: run %s - alive %d, rounds played %d, champion: %s, finished: %s"
        % (
            out["run"], out["alive"], out["rounds_played"],
            out["champion"] or "none", "yes" if out["finished"] else "no",
        )
    ]
    final = out["final"]
    if final and final.get("result"):
        res = final["result"]
        lines.append(
            "final: %s is better (champion %s vs baseline %s) %s"
            % (res.get("better"), res.get("champion_total"),
               res.get("baseline_total"), (res.get("reason") or "").strip())
        )
    elif final:
        lines.append("final: pending (judge verdict not collected yet)")
    lines.append("collected verdicts: %d" % len(out["collected"]))
    for item in out["collected"]:
        lines.append(
            "  %s: %s%s" % (item["match"], item["status"],
                            (" - " + item["note"]) if item["note"] else "")
        )
    return "\n".join(lines)


def _handle_cancel(rest: List[str]) -> str:
    _positionals, flags = _parse_flags(rest, {"run": str, "dir": str, "purge": bool})
    out = tools.arena_cancel(flags)
    if not out.get("ok"):
        return "arena cancel: %s" % out.get("error")
    return "arena cancel: run %s cancelled (purged: %s)" % (
        out["run"], "yes" if out.get("purged") else "no",
    )


_HANDLERS = {
    "ping": _handle_ping,
    "plan": _handle_plan,
    "start": _handle_start,
    "status": _handle_status,
    "result": _handle_result,
    "cancel": _handle_cancel,
}


def _usage() -> str:
    return (
        "arena: unknown or missing subcommand. Known: %s.\n"
        'Examples: /arena ping | /arena plan | /arena start "<task>" | '
        "/arena status | /arena result | /arena cancel"
        % ", ".join(KNOWN_SUBCOMMANDS)
    )

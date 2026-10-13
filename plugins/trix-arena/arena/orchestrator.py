#!/usr/bin/env python3
"""Deterministic orchestrator for the Trix Arena plugin (stage 3).

Drives the tournament core (``arena.core``) job by job:

- **Waves with bounded parallelism** — jobs come out of ``core.phase_jobs``
  in deterministic order and are chunked into batches of at most
  ``state["wave"]`` jobs (``wave_batches`` / ``effective_wave``). Stage 3
  executes the batches sequentially; a stage-4 runner may execute the jobs
  inside one batch concurrently without changing any other semantics.
- **Retry policy** — only :class:`JobTransportError` (transport problems)
  and :class:`JobSchemaError` (the backend returned a payload that fails
  its JSON schema, is not decidable as a judge verdict, or echoes a wrong
  idempotency key) are retried, up to ``MAX_RETRIES`` retries per job.
  Any other backend exception fails the job immediately (no retry).
- **Idempotency** — every job gets a deterministic idempotency key
  (:func:`idempotency_key`); the key is embedded in the job prompt, may be
  echoed back by the backend (a mismatch is a schema error), and is
  recorded in ``state["orchestration"]["completed"]`` once the job's
  outputs are durably written.
- **State after every successful job** — the run state (including the
  orchestration record) is saved through ``core.save_state`` after each
  successful job, so a crash between jobs never loses completed work;
  ``core.missing_jobs`` makes already-completed jobs non-re-executable.
- **pause / cancel / resume** — a small cooperative state machine stored
  in ``state["orchestration"]["status"]``: ``idle -> running -> done``,
  with ``paused`` (resumable) and ``cancelled`` (terminal). Pause and
  cancel take effect at job boundaries; the in-flight job always
  completes and is saved first.

The orchestrator is a library: it never makes network calls and needs a
callable ``backend(job) -> payload dict`` provided by the caller. Wiring
a real (or fake) runner and user-facing tools is stage 4. Everything is
confined to the run directory exactly like the core: relative
``state["dir"]``, prompt and output files under it, state in
``<run>/arena.json`` written atomically by the core.

Job kinds follow ``core.phase_jobs`` exactly: ``competitor`` (spawn),
``attacker`` (attack), ``defender`` (defend), ``judge`` (judge) and
``final`` (final judge); payloads are validated against
``schemas.JOB_SCHEMAS``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Sequence

try:  # imported as trix_arena.arena.orchestrator (normal package layout)
    from .. import schemas
    from . import core
except ImportError:  # imported as top-level `arena` package (tests, plugin host)
    import schemas
    from . import core

__all__ = [
    "ORCHESTRATION_KEY",
    "STATUS_IDLE",
    "STATUS_RUNNING",
    "STATUS_PAUSED",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUSES",
    "MAX_RETRIES",
    "RETRYABLE_CLASSES",
    "JobTransportError",
    "JobSchemaError",
    "ensure_orchestration",
    "status_of",
    "pause",
    "cancel",
    "resume",
    "idempotency_key",
    "effective_wave",
    "wave_batches",
    "ensure_prompt",
    "execute_job",
    "run_phase",
    "run",
]

# ---------------------------------------------------------------------------
# Constants and errors
# ---------------------------------------------------------------------------

#: State field (added to the core state dict) holding the orchestration
#: record. Additive: the core never reads it, so older states migrate
#: transparently.
ORCHESTRATION_KEY = "orchestration"

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"
STATUS_DONE = "done"

#: id -> paused -> running -> done; cancelled is reachable from every
#: non-terminal status and is terminal.
STATUSES = (STATUS_IDLE, STATUS_RUNNING, STATUS_PAUSED, STATUS_CANCELLED, STATUS_DONE)
TERMINAL = (STATUS_CANCELLED, STATUS_DONE)

#: Retries per job (after the initial attempt) for retryable errors only.
MAX_RETRIES = 2

#: Verdict fields copied from a judge/final payload into the verdict file
#: (the exact shape ``core.decide`` / ``core.collect`` read).
_VERDICT_KEYS = ("winner", "reason", "survived", "standing", "scores", "fixed")


class JobTransportError(Exception):
    """Transport-level backend failure (retryable).

    The stage-4 runner raises this for connection errors, timeouts and
    rate limits; tests raise it to simulate flaky infrastructure.
    """


class JobSchemaError(Exception):
    """The backend returned an unusable payload (retryable).

    Raised when the payload fails ``schemas.payload_error``, is not a JSON
    object, echoes a wrong idempotency key, or is a judge verdict that
    ``core.decide`` rejects. The job's outputs are never written.
    """


#: Exception classes the orchestrator retries; anything else fails fast.
#: Defined here, below both classes, so the tuple resolves at import time.
RETRYABLE_CLASSES = (JobTransportError, JobSchemaError)


# ---------------------------------------------------------------------------
# Orchestration record (state["orchestration"])
# ---------------------------------------------------------------------------


def ensure_orchestration(state: Dict[str, Any]) -> Dict[str, Any]:
    """Return the orchestration record, creating/repairing it in place.

    Idempotent and additive: missing fields get their defaults, an unknown
    stored status falls back to ``idle``. Never saves state itself.
    """
    rec = state.get(ORCHESTRATION_KEY)
    if not isinstance(rec, dict):
        rec = {}
        state[ORCHESTRATION_KEY] = rec
    defaults: Dict[str, Any] = {
        "schema_version": 1,
        "status": STATUS_IDLE,
        "phase": None,
        "completed": {},   # job id -> idempotency key
        "retries": {},     # job id -> {"transport": n, "schema": n, "attempts": n}
        "failures": {},    # job id -> {"class": ..., "error": ..., "attempts": n}
        "jobs_done": 0,    # cumulative completed jobs
        "last_error": None,
    }
    for key, value in defaults.items():
        rec.setdefault(key, value)
    if rec["status"] not in STATUSES:
        rec["status"] = STATUS_IDLE
    for name in ("completed", "retries", "failures"):
        if not isinstance(rec[name], dict):
            rec[name] = {}
    for name in ("jobs_done",):
        if not isinstance(rec[name], int) or isinstance(rec[name], bool):
            rec[name] = 0
    return rec


def _touch(rec: Dict[str, Any]) -> None:
    rec["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")


def status_of(state: Dict[str, Any]) -> str:
    """Current orchestration status of ``state`` (creates the record)."""
    return ensure_orchestration(state)["status"]


def _fail(rec: Dict[str, Any], message: str) -> None:
    rec["last_error"] = message
    rec["status"] = STATUS_PAUSED


def pause(state: Dict[str, Any]) -> Dict[str, Any]:
    """Request a cooperative pause (takes effect at the next job boundary).

    Allowed from ``idle``, ``running`` and ``paused`` (no-op); terminal
    statuses raise :class:`core.ArenaError`. Saves state.
    """
    rec = ensure_orchestration(state)
    if rec["status"] in TERMINAL:
        raise core.ArenaError("cannot pause a %s orchestration" % rec["status"])
    if rec["status"] != STATUS_PAUSED:
        rec["status"] = STATUS_PAUSED
        _touch(rec)
        core.save_state(state)
    return rec


def cancel(state: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel the orchestration (terminal). Saves state.

    Completed jobs and their outputs are kept for audit; the tournament
    core state itself is untouched (``arena_cancel`` remains the tool to
    purge a run).
    """
    rec = ensure_orchestration(state)
    if rec["status"] == STATUS_DONE:
        raise core.ArenaError("cannot cancel a finished orchestration")
    if rec["status"] != STATUS_CANCELLED:
        rec["status"] = STATUS_CANCELLED
        rec["last_error"] = None
        _touch(rec)
        core.save_state(state)
    return rec


def resume(state: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a paused orchestration (``paused -> running``).

    Idempotent for an already-``running`` orchestration (crash recovery:
    a process that died mid-run leaves ``running`` on disk). Terminal
    statuses and ``idle`` raise :class:`core.ArenaError`. Saves state.
    """
    rec = ensure_orchestration(state)
    if rec["status"] == STATUS_RUNNING:
        return rec
    if rec["status"] in TERMINAL:
        raise core.ArenaError("cannot resume a %s orchestration" % rec["status"])
    if rec["status"] != STATUS_PAUSED:
        raise core.ArenaError(
            "cannot resume from status %r (only %r)" % (rec["status"], STATUS_PAUSED)
        )
    rec["status"] = STATUS_RUNNING
    _touch(rec)
    core.save_state(state)
    return rec


# ---------------------------------------------------------------------------
# Idempotency keys
# ---------------------------------------------------------------------------


def idempotency_key(state: Dict[str, Any], job: Dict[str, Any]) -> str:
    """Deterministic idempotency key for one job.

    Format: ``sha256("arena-job|v<state schema>|<seed>|<kind>|<job id>|"
    "<';'-joined output basenames>")[:32]``.

    Inputs are deliberately machine-independent (no absolute paths, no
    timestamps, no run-directory name), so the key survives a crash, a
    reload from disk, or moving the run directory.
    """
    outputs = ";".join(
        os.path.basename(os.path.normpath(str(p))) for p in (job.get("outputs") or [])
    )
    parts = (
        "arena-job",
        "v%s" % state.get("state_schema_version", core.CURRENT_STATE_SCHEMA_VERSION),
        str(state.get("seed")),
        str(job.get("kind")),
        str(job.get("id")),
        outputs,
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Waves
# ---------------------------------------------------------------------------


def effective_wave(state: Dict[str, Any]) -> int:
    """Concurrency cap for one wave: ``state["wave"]`` or the core default."""
    value = state.get("wave")
    if value is None:
        return core.DEFAULT_WAVE
    if isinstance(value, bool):
        raise core.ArenaError("wave must be a positive integer, got a boolean")
    try:
        wave = int(value)
    except (TypeError, ValueError):
        raise core.ArenaError("wave must be a positive integer, got %r" % (value,)) from None
    if wave < 1:
        raise core.ArenaError("wave must be a positive integer, got %r" % (value,))
    return wave


def wave_batches(jobs: Sequence[Dict[str, Any]], wave: int) -> List[List[Dict[str, Any]]]:
    """Split ``jobs`` (deterministic order) into waves of at most ``wave``.

    The last wave may be smaller. Raises :class:`core.ArenaError` when
    ``wave < 1``. Job order inside waves is preserved so retries and
    re-runs execute jobs identically.
    """
    if isinstance(wave, bool) or not isinstance(wave, int) or wave < 1:
        raise core.ArenaError("wave must be a positive integer, got %r" % (wave,))
    jobs = list(jobs)
    return [jobs[i:i + wave] for i in range(0, len(jobs), wave)]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _read_out(state: Dict[str, Any], rel: str) -> str:
    """Read a run-relative text file for prompt embedding ('(missing)' safe)."""
    path = os.path.join(state["dir"], rel)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
    except OSError:
        return "(missing)"
    return text or "(empty)"


def _card_lines(state: Dict[str, Any], agent_id: str) -> str:
    agent = (state.get("agents") or {}).get(agent_id) or {}
    card = agent.get("card") or {}
    rows = []
    for key in ("reasoning", "workflow", "strategy"):
        part = card.get(key) or {}
        rows.append("%s: %s — %s" % (key, part.get("name", "?"), part.get("how", "?")))
    return "\n".join(rows)


def _task_text(state: Dict[str, Any]) -> str:
    return state.get("task") or "(no task text was provided)"


def _match_solution(state: Dict[str, Any], rnd: int, match_id: str, who: str) -> str:
    """Current solution of ``who``: this round's revision if present, else spawn."""
    revised = "%s/%s.%s.solution.md" % (rnd, match_id, who)
    path = os.path.join(state["dir"], revised)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return _read_out(state, revised)
    return _read_out(state, "r0/%s.md" % who)


def _final_y_text(state: Dict[str, Any]) -> str:
    """Baseline text for the final: a path is read, otherwise used verbatim."""
    baseline = state.get("baseline")
    if isinstance(baseline, str) and baseline.strip():
        path = os.path.join(state["dir"], baseline)
        if os.path.isfile(path):
            return _read_out(state, baseline)
        return baseline.strip()
    return "(baseline unavailable)"


def _prompt_text(state: Dict[str, Any], job: Dict[str, Any]) -> str:
    kind = job["kind"]
    key = job.get("idempotency_key") or idempotency_key(state, job)
    head = (
        "Trix Arena run  |  job %s  |  kind %s  |  round %s\n"
        "Idempotency key: %s\n"
        "Task:\n%s\n"
        % (job["id"], kind, job.get("round"), key, _task_text(state))
    )
    if kind == "competitor":
        return (
            head
            + "\nYou are competitor %s. Your strategy card:\n%s\n\n"
              "Solve the task above as best you can, following your card.\n"
              "Reply with one JSON object: {\"solution\": \"<your complete answer>\", "
              "\"idempotency_key\": \"%s\"}.\n"
              % (job["agent"], _card_lines(state, job["agent"]), key)
        )
    if kind == "attacker":
        return (
            head
            + "\nYou are the attacker %s against opponent %s in match %s.\n"
              "Your strategy card:\n%s\n"
              "Opponent's solution:\n%s\n\n"
              "Find the concrete weaknesses in the opponent's solution.\n"
              "Reply with one JSON object: {\"attack\": \"<the weaknesses>\", "
              "\"idempotency_key\": \"%s\"}.\n"
              % (job["agent"], job["opponent"], job["match"],
                 _card_lines(state, job["agent"]),
                 _read_out(state, "r0/%s.md" % job["opponent"]), key)
        )
    if kind == "defender":
        return (
            head
            + "\nYou are the defender %s in match %s.\n"
              "Your strategy card:\n%s\n"
              "Your solution:\n%s\n"
              "Attacks against you:\n%s\n\n"
              "Rebut the attacks and improve your solution.\n"
              "Reply with one JSON object: {\"defense\": \"<your rebuttal>\", "
              "\"revised_solution\": \"<your full improved solution>\", "
              "\"idempotency_key\": \"%s\"}.\n"
              % (job["agent"], job["match"], _card_lines(state, job["agent"]),
                 _read_out(state, "r0/%s.md" % job["agent"]),
                 _read_out(state, "%s/%s.%s.attack.md" % (job["round"], job["match"], job["agent"])),
                 key)
        )
    if kind == "judge":
        return _judge_prompt(state, job, key)
    if kind == "final":
        champion = state.get("champion")
        return (
            head
            + "\nFINAL MATCH — solution x is the tournament champion (%s), "
              "solution y is the human baseline.\n"
              "Solution x:\n%s\n"
              "Solution y:\n%s\n\n"
              "Judge fairly; the winner may be x or y.\n"
              "Reply with one JSON object: {\"winner\": \"x\" or \"y\", "
              "\"scores\": {\"x\": {...}, \"y\": {...}}, \"reason\": \"...\", "
              "\"survived\": [...], \"standing\": {...}, \"fixed\": [...]}, "
              "echoing \"idempotency_key\": \"%s\".\n"
              % (champion, _read_out(state, "r0/%s.md" % (champion or "",)),
                 _final_y_text(state), key)
        )
    raise core.ArenaError("unknown job kind: %r" % (kind,))


def _judge_prompt(state: Dict[str, Any], job: Dict[str, Any], key: str) -> str:
    head = (
        "Trix Arena run  |  job %s  |  kind judge  |  round %s\n"
        "Idempotency key: %s\n"
        "Task:\n%s\n"
        % (job["id"], job.get("round"), key, _task_text(state))
    )
    lines = [
        head,
        "\nYou are the judge of match %s between %s and %s."
        % (job["match"], job["a"], job["b"]),
        "\nSolution a (%s):\n%s" % (job["a"], _match_solution(state, job["round"], job["match"], job["a"])),
    ]
    if job.get("b"):
        lines.append("\nSolution b (%s):\n%s" % (job["b"], _match_solution(state, job["round"], job["match"], job["b"])))
        for who in (job["a"], job["b"]):
            lines.append("\nAttacks on %s:\n%s" % (
                who, _read_out(state, "%s/%s.%s.attack.md" % (job["round"], job["match"], who))))
            lines.append("\nDefense by %s:\n%s" % (
                who, _read_out(state, "%s/%s.%s.defense.md" % (job["round"], job["match"], who))))
    else:
        lines.append(
            "\nThis is a bye: %s has no opponent this round. Return a verdict "
            "naming %s the winner with full scores." % (job["a"], job["a"])
        )
    lines.append(
        "\nReply with one JSON object: {\"winner\": \"%s\" or \"%s\", "
        "\"scores\": {\"%s\": {...}, \"%s\": {...}}, \"reason\": \"...\", "
        "\"survived\": [...], \"standing\": {...}}, echoing "
        "\"idempotency_key\": \"%s\".\n"
        % (job["a"], job["b"], job["a"], job["b"], key)
    )
    return "\n".join(lines)


def ensure_prompt(state: Dict[str, Any], job: Dict[str, Any]) -> str:
    """Materialise the job's prompt file (idempotent) and return its path.

    The path is ``job["prompt"]`` from the core — always inside the run
    directory. Existing non-empty prompts are kept so re-runs never
    rewrite history.
    """
    path = job["prompt"]
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    text = _prompt_text(state, dict(job, idempotency_key=idempotency_key(state, job)))
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


def _check_decidable(job: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Dry-run ``core.decide`` so an undecidable verdict never reaches disk."""
    verdict = {k: payload[k] for k in _VERDICT_KEYS if k in payload}
    if job["kind"] == "final":
        a, b = "x", "y"
    else:
        a, b = job.get("a"), job.get("b")
    try:
        core.decide(verdict, a, b)
    except core.ArenaError as exc:
        raise JobSchemaError(
            "%s: judge verdict is not decidable: %s" % (job["id"], exc)
        ) from None


def _write_outputs(job: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Write the job's output files (atomically, one per ``job["outputs"]``)."""
    kind = job["kind"]
    if kind == "competitor":
        texts: List[str] = [payload["solution"]]
    elif kind == "attacker":
        texts = [payload["attack"]]
    elif kind == "defender":
        texts = [payload["defense"], payload["revised_solution"]]
    else:  # judge / final
        verdict = {k: payload[k] for k in _VERDICT_KEYS if k in payload}
        texts = [json.dumps(verdict, indent=1, sort_keys=True) + "\n"]
    outputs = list(job["outputs"])
    if len(texts) != len(outputs):
        raise JobSchemaError(
            "%s: payload produced %d output text(s) but the job lists %d output file(s)"
            % (job["id"], len(texts), len(outputs))
        )
    for path, text in zip(outputs, texts):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)


def execute_job(
    state: Dict[str, Any], job: Dict[str, Any], backend: Callable[[Dict[str, Any]], Any]
) -> Dict[str, Any]:
    """Run one job through ``backend`` and durably write its outputs.

    Raises :class:`JobTransportError` / :class:`JobSchemaError` (retryable)
    or any non-retryable backend exception. On success the job is absent
    from ``core.missing_jobs`` forever (outputs exist and are non-empty).
    """
    if not callable(backend):
        raise core.ArenaError(
            "orchestrator backend must be callable: backend(job) -> payload dict"
        )
    key = idempotency_key(state, job)
    ensure_prompt(state, job)
    envelope = dict(job)
    envelope["idempotency_key"] = key
    payload = backend(envelope)
    if not isinstance(payload, dict):
        raise JobSchemaError(
            "%s: backend returned %s, expected a JSON object payload"
            % (job["id"], type(payload).__name__)
        )
    message = schemas.payload_error(job["kind"], payload)
    if message:
        raise JobSchemaError("%s: %s" % (job["id"], message))
    echo = payload.get("idempotency_key")
    if echo is not None and str(echo) != key:
        raise JobSchemaError(
            "%s: payload idempotency key mismatch (expected %s, got %r)"
            % (job["id"], key, echo)
        )
    if job["kind"] in ("judge", "final"):
        _check_decidable(job, payload)
    _write_outputs(job, payload)
    return {
        "job": job["id"],
        "kind": job["kind"],
        "idempotency_key": key,
        "outputs": list(job["outputs"]),
    }


# ---------------------------------------------------------------------------
# Phase runner and main loop
# ---------------------------------------------------------------------------


def run_phase(
    state: Dict[str, Any],
    phase: str,
    backend: Callable[[Dict[str, Any]], Any],
    save: Callable[[Dict[str, Any]], None] = core.save_state,
    max_retries: int = MAX_RETRIES,
) -> Dict[str, Any]:
    """Run every still-missing job of one phase; return a phase summary.

    Honours pause/cancel at job boundaries, retries only retryable errors,
    and saves state after every successful job.
    """
    rec = ensure_orchestration(state)
    if rec["status"] == STATUS_CANCELLED:
        raise core.ArenaError("orchestration is cancelled")
    if rec["status"] == STATUS_DONE:
        raise core.ArenaError("orchestration is finished")
    summary: Dict[str, Any] = {"phase": phase, "done": 0, "retries": 0, "failed": []}
    jobs = core.missing_jobs(core.phase_jobs(state, phase))
    wave = effective_wave(state)
    rec["phase"] = phase
    _touch(rec)
    save(state)
    for batch in wave_batches(jobs, wave):
        for job in batch:
            if rec["status"] != STATUS_RUNNING and rec["status"] != STATUS_IDLE:
                summary["status"] = rec["status"]
                return summary
            job_id = job["id"]
            attempts = 0
            while True:
                attempts += 1
                try:
                    info = execute_job(state, job, backend)
                    break
                except RETRYABLE_CLASSES as exc:  # type: ignore[misc]
                    kind_txt = "transport" if isinstance(exc, JobTransportError) else "schema"
                    tally = rec["retries"].setdefault(
                        job_id, {"transport": 0, "schema": 0, "attempts": 0}
                    )
                    tally["attempts"] = attempts
                    summary["retries"] += 1
                    if attempts > max_retries:
                        rec["failures"][job_id] = {
                            "class": kind_txt,
                            "error": str(exc)[:500],
                            "attempts": attempts,
                        }
                        _fail(rec, "%s: %s" % (job_id, exc))
                        summary["failed"].append(
                            {"job": job_id, "class": kind_txt, "error": str(exc)[:200]}
                        )
                        summary["status"] = rec["status"]
                        save(state)
                        return summary
                    # a retry is only counted when one is actually
                    # scheduled: MAX_RETRIES retries => MAX_RETRIES + 1
                    # attempts, with the kind counters summing to the
                    # retries used (the first attempt is never a retry).
                    tally[kind_txt] += 1
                    continue
                except Exception as exc:  # non-retryable backend failure
                    rec["failures"][job_id] = {
                        "class": "backend",
                        "error": repr(exc)[:500],
                        "attempts": attempts,
                    }
                    _fail(rec, "%s: %s" % (job_id, exc))
                    summary["failed"].append(
                        {"job": job_id, "class": "backend", "error": repr(exc)[:200]}
                    )
                    summary["status"] = rec["status"]
                    save(state)
                    return summary
            rec["completed"][job_id] = info["idempotency_key"]
            rec["failures"].pop(job_id, None)
            rec["jobs_done"] += 1
            summary["done"] += 1
            _touch(rec)
            save(state)  # durable after EVERY successful job
    summary["status"] = rec["status"]
    return summary


def run(
    state: Dict[str, Any],
    backend: Callable[[Dict[str, Any]], Any],
    save: Callable[[Dict[str, Any]], None] = core.save_state,
) -> Dict[str, Any]:
    """Drive the whole tournament: spawn -> rounds -> final -> done.

    Returns a run summary dict (``status``, ``champion``, ``finished``,
    per-phase summaries, totals). Refuses to start from ``paused``
    (call :func:`resume`) or ``cancelled``; a ``done`` orchestration is a
    no-op. A failing job (retries exhausted, or any non-retryable backend
    error) pauses the run with ``last_error`` set; completed jobs and
    their outputs are never lost or recomputed, so no double winners can
    be produced across crash/resume cycles.
    """
    rec = ensure_orchestration(state)
    if rec["status"] == STATUS_CANCELLED:
        raise core.ArenaError("orchestration is cancelled; cancelled is terminal")
    if rec["status"] == STATUS_PAUSED:
        raise core.ArenaError("orchestration is paused; call resume() first")
    if state.get("cancelled"):
        raise core.ArenaError("the run itself is cancelled (state flag)")
    if backend is None:
        raise core.ArenaError(
            "orchestrator needs a callable backend(job) -> payload dict; "
            "the real runner is wired in stage 4"
        )
    phases: List[Dict[str, Any]] = []
    jobs_done = 0
    retries = 0
    failed: List[Dict[str, Any]] = []

    def note(phase_summary: Dict[str, Any]) -> None:
        phases.append(phase_summary)
        nonlocal jobs_done, retries
        jobs_done += phase_summary["done"]
        retries += phase_summary["retries"]
        failed.extend(phase_summary["failed"])

    def summary() -> Dict[str, Any]:
        final = state.get("final") or {}
        return {
            "status": rec["status"],
            "champion": state.get("champion"),
            "finished": bool(state.get("champion"))
            and (not final or final.get("result")),
            "phases": phases,
            "jobs_done": jobs_done,
            "retries": retries,
            "failed": failed,
            "last_error": rec.get("last_error"),
        }

    if rec["status"] == STATUS_DONE:
        return summary()  # idempotent no-op: nothing left to execute

    rec["status"] = STATUS_RUNNING
    rec["phase"] = None
    _touch(rec)
    save(state)

    note(run_phase(state, "spawn", backend, save=save))
    if rec["status"] != STATUS_RUNNING:
        return summary()

    agents_n = int(state.get("agents_n") or len(state.get("agents") or {}))
    max_rounds = len(core.bracket_sizes(agents_n)) + 4
    for _ in range(max_rounds):
        if state.get("champion"):
            break
        if core.current_round(state) is None:
            break
        for phase in ("attack", "defend", "judge"):
            note(run_phase(state, phase, backend, save=save))
            if rec["status"] != STATUS_RUNNING:
                return summary()
        still_missing = core.missing_jobs(core.phase_jobs(state, "judge"))
        if still_missing:
            _fail(
                rec,
                "judge verdicts still missing after the judge phase: %s"
                % ", ".join(job["id"] for job in still_missing),
            )
            save(state)
            return summary()
        core.collect(state)  # decide every open match from its verdict file
        round_obj = core.current_round(state)
        if round_obj and all(match["winner"] for match in round_obj["matches"]):
            core.advance(state)  # close round, open next / crown champion
        save(state)
    else:
        _fail(rec, "orchestrator did not converge after %d rounds" % max_rounds)
        save(state)
        return summary()

    if not state.get("champion"):
        _fail(rec, "tournament ended without a champion")
        save(state)
        return summary()

    final = state.get("final")
    if final is not None and not final.get("result"):
        note(run_phase(state, "final", backend, save=save))
        if rec["status"] != STATUS_RUNNING:
            return summary()
        if core.missing_jobs(core.phase_jobs(state, "final")):
            _fail(rec, "final verdict still missing after the final phase")
            save(state)
            return summary()
        core.collect(state)  # decide x/y and finish the run
        save(state)

    rec["phase"] = None
    rec["status"] = STATUS_DONE
    _touch(rec)
    save(state)
    return summary()

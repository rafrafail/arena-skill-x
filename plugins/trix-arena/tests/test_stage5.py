#!/usr/bin/env python3
"""Stage 5 tests: tournament invariants, simulated E2E runs, failure
recovery and security hardening — all through the REAL core + orchestrator
against a deterministic FAKE backend (no network, no API keys, no LLM).

Complements (does not duplicate) the existing suites:

- tests/test_core.py  — core-level units (deal/bracket/bye/scoring/
  migrations/advance) and core-level 100-agent simulations;
- tests/test_stage2.py — tools/schemas surface and lifecycle;
- tests/test_stage3.py — orchestrator retry/status/resume/idempotency
  at 4-agent scale;
- tests/test_stage4.py — SKILL.md documentation pins.

Stage-5 coverage added here:

(a) Invariants parameterised over sizes [1, 2, 3, 7, 8, 16, 32, 100]:
    no duplicate strategy cards, exactly one winner, eliminated agents
    never return, match graph consistent with ``core.plan_rows``.
(b) Simulated E2E for sizes [1, 2, 7, 8, 16, 100]: final status ``done``,
    backend call count == ``core.plan_totals(n).calls`` (never hardcoded),
    champion is a valid participant, every spawn output on disk, and
    same-seed determinism.
(c) Failure paths: transport timeout -> retries -> paused -> resume
    finishes; undecidable judge verdict -> JobSchemaError exhaustion;
    non-object payload -> JobSchemaError; provider error -> class
    "backend", single attempt; cancel mid-run is terminal; restart after
    KeyboardInterrupt preserves state and never re-executes completed
    jobs.
(d) Security: run-id path traversal rejected by the tool layer; oversized
    task (schema declares no cap) round-trips verbatim and stays inside
    the run directory; malicious participant payloads (traversal strings,
    HTML, hostile JSON) never escape the run directory and never corrupt
    verdicts; tampered verdict file -> controlled pause with ``.unreadable``
    audit copy; corrupt arena.json detected by load_state; orchestration
    record tampering self-heals.

Contract lessons from stage 3 are baked in: judge payloads require exactly
``winner`` + ``scores`` (empty object valid); the idempotency-key echo is
allowed and a mismatch is a JobSchemaError; retryable classes are
JobTransportError/JobSchemaError up to MAX_RETRIES=2 retries (3 attempts),
then paused + failures; other exceptions are class "backend" with a single
attempt; KeyboardInterrupt is never retried; cancelled is terminal.

All asserted strings/keys were read from the actual sources this stage:
arena/orchestrator.py, arena/core.py, schemas.py, tools.py.

NOTE (honest): these tests were written without a shell — the worker never
executed them. The host gatekeeper runs ``python3 -m compileall plugins``
and ``pytest``; on red, fix the TESTS, never core/orchestrator (stage-3
precedent).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import trix_arena  # noqa: F401  (registered as a package by tests/conftest.py)
from trix_arena import schemas, tools
from trix_arena.arena import core, orchestrator as orch

WAVE = 10
INVAR_SIZES = (1, 2, 3, 7, 8, 16, 32, 100)
E2E_SIZES = (1, 2, 7, 8, 16, 100)

_DATA = None


def _strategies():
    """Strategy card pool (cached); upstream default source as in test_core."""
    global _DATA
    if _DATA is None:
        _DATA = core.load_strategies()
    return _DATA


# ---------------------------------------------------------------------------
# Deterministic fake backend + full-ladder harness
# ---------------------------------------------------------------------------


def _fake_payload(job):
    """Deterministic payload for any job kind (schema-valid, echo-ready)."""
    kind = job["kind"]
    if kind == "competitor":
        return {"solution": "solution of %s" % job["agent"]}
    if kind == "attacker":
        return {"attack": "attacks by %s on %s" % (job["agent"], job["opponent"])}
    if kind == "defender":
        return {
            "defense": "rebuttal by %s" % job["agent"],
            "revised_solution": "revised solution of %s" % job["agent"],
        }
    if kind == "judge":
        return {"winner": job["a"], "reason": "fake pick", "survived": [], "scores": {}}
    if kind == "final":
        return {"winner": "x", "reason": "champion prevails", "survived": [], "scores": {}}
    raise core.ArenaError("fake backend: unknown job kind %r" % (kind,))


class FakeArenaBackend:
    """Callable backend(job-envelope) -> payload dict.

    - echoes the envelope's ``idempotency_key`` (as the real runner will);
    - ``on_attempt(job, call_no)`` may return an override payload or raise;
    - ``on_call(envelope)`` runs before payload construction (may raise
      KeyboardInterrupt or pause/cancel the orchestration cooperatively).
    """

    def __init__(self, on_attempt=None, on_call=None):
        self.on_attempt = on_attempt
        self.on_call = on_call
        self.calls = 0
        self.attempts = []  # job ids in call order

    def __call__(self, envelope):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(envelope)
        job = envelope
        self.attempts.append(job["id"])
        payload = _fake_payload(job)
        key = envelope.get("idempotency_key")
        if key is not None:
            payload["idempotency_key"] = key
        if self.on_attempt is not None:
            override = self.on_attempt(job, self.calls)
            if override is not None:
                return override
        return payload

    def attempted(self, job_id):
        return self.attempts.count(job_id)


class _Ladder:
    """One complete fake-backend tournament in its own temp directory.

    Kept alive (and cached) for the whole test session so invariant and
    E2E tests share a single run per size instead of re-running it.
    """

    def __init__(self, n, seed, wave=WAVE, has_baseline=False):
        self.n = n
        self.seed = seed
        self.wave = wave
        self.base = tempfile.mkdtemp(prefix="arena-s5-ladder-")
        prev = os.getcwd()
        os.chdir(self.base)
        try:
            self.rel_run = os.path.join("runs", "s5-%s" % seed)
            self.state = core.new_state(
                n, seed, _strategies(), self.rel_run,
                wave=wave, has_baseline=has_baseline,
            )
            self.state["task"] = "stage 5 fake-backend task"
            self.backend = FakeArenaBackend()
            self.summary = orch.run(self.state, self.backend)
        finally:
            os.chdir(prev)

    def path(self, *rel):
        return os.path.join(self.base, self.rel_run, *rel)


_LADDERS = {}


def _ladder(n, seed="stage5", wave=WAVE):
    key = (n, seed, wave)
    if key not in _LADDERS:
        _LADDERS[key] = _Ladder(n, seed, wave=wave)
    return _LADDERS[key]


def _probe(n, seed, wave=WAVE):
    """Run one throwaway ladder and return its observable fingerprint."""
    ladder = _Ladder(n, seed, wave=wave)
    state = ladder.state
    fingerprint = {
        "champion": state["champion"],
        "winners": [m["winner"] for rd in state["rounds"] for m in rd["matches"]],
        "calls": ladder.backend.calls,
    }
    shutil.rmtree(ladder.base, ignore_errors=True)
    return fingerprint


class TmpCwdTestCase(unittest.TestCase):
    """Run the test inside a throwaway working directory (stage-2 pattern)."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp(prefix="arena-s5-fail-")
        os.chdir(self._tmp)

    def tearDown(self):
        os.chdir(self._prev_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def fresh_state(self, n, name, wave=WAVE):
        rel = os.path.join("runs", name)
        state = core.new_state(n, name, _strategies(), rel, wave=wave)
        state["task"] = "stage 5 failure/security task"
        return state


# ---------------------------------------------------------------------------
# (a) Invariants across sizes
# ---------------------------------------------------------------------------


class InvariantsTest(unittest.TestCase):
    """PLAN invariant: no duplicate cards, exactly one winner, eliminated
    agents never return — parameterised over ladder sizes."""

    def test_invariants_hold_across_sizes(self):
        for n in INVAR_SIZES:
            with self.subTest(agents=n):
                entry = _ladder(n)
                state, summary = entry.state, entry.summary
                agents = state["agents"]
                self.assertEqual(orch.STATUS_DONE, summary["status"])

                # exactly one winner: champion alive, everyone else out
                self.assertTrue(state["champion"])
                alive = [aid for aid, a in agents.items() if a["alive"]]
                self.assertEqual([state["champion"]], alive)
                self.assertEqual(n - 1, sum(1 for a in agents.values() if not a["alive"]))

                # no duplicate strategy cards anywhere in the deal
                triples = set()
                for aid in agents:
                    card = agents[aid]["card"]
                    triples.add(tuple(card[part]["id"] for part in
                                      ("reasoning", "workflow", "strategy")))
                self.assertEqual(n, len(triples))

                # eliminated agents never come back
                elim = {aid: a["eliminated_in"] for aid, a in agents.items()
                        if not a["alive"]}
                self.assertEqual(n - 1, len(elim))
                seen_match_ids = set()
                for rd in state["rounds"]:
                    for m in rd["matches"]:
                        self.assertNotIn(m["id"], seen_match_ids)
                        seen_match_ids.add(m["id"])
                        self.assertIn(m["winner"], (m["a"], m["b"]))
                        loser = m["b"] if m["winner"] == m["a"] else m["a"]
                        self.assertEqual(loser, m["loser"])
                        for side in (m["a"], m["b"]):
                            ei = agents[side]["eliminated_in"]
                            self.assertTrue(
                                ei is None or ei >= rd["n"],
                                "%s: side %s plays in round %s but was "
                                "eliminated in %s" % (m["id"], side, rd["n"], ei),
                            )
                        self.assertEqual(rd["n"], agents[loser]["eliminated_in"])
                        self.assertEqual(m["winner"], agents[loser]["eliminated_by"])
                        self.assertTrue(
                            agents[m["winner"]]["alive"]
                            or agents[m["winner"]]["eliminated_in"] > rd["n"]
                        )
                self.assertEqual(n - 1, sum(len(rd["matches"])
                                            for rd in state["rounds"]))
                if n >= 2:
                    last = state["rounds"][-1]
                    self.assertEqual(1, len(last["matches"]))
                    self.assertEqual(state["champion"], last["matches"][0]["winner"])
                for aid, a in agents.items():
                    self.assertLessEqual(a["byes"], 1)
                    if aid == state["champion"]:
                        self.assertTrue(a["alive"])
                        self.assertIsNone(a["eliminated_by"])
                    else:
                        self.assertFalse(a["alive"])
                        self.assertIn(a["eliminated_by"], agents)


# ---------------------------------------------------------------------------
# (b) Simulated E2E runs
# ---------------------------------------------------------------------------


class E2ETest(unittest.TestCase):
    """Full fake-backend ladders match ``core.plan_totals`` exactly."""

    def test_e2e_matches_plan_totals(self):
        for n in E2E_SIZES:
            with self.subTest(agents=n):
                entry = _ladder(n)
                state, summary, backend = entry.state, entry.summary, entry.backend
                rec = state[orch.ORCHESTRATION_KEY]
                totals = core.plan_totals(n, entry.wave)
                rows = core.plan_rows(n, entry.wave)

                self.assertEqual(orch.STATUS_DONE, summary["status"])
                self.assertEqual(orch.STATUS_DONE, rec["status"])
                self.assertTrue(summary["finished"])
                self.assertTrue(state["champion"])
                self.assertIn(state["champion"], state["agents"])
                self.assertEqual(state["champion"], summary["champion"])
                self.assertEqual([], summary["failed"])
                self.assertEqual(0, summary["retries"])

                # call accounting comes from plan_totals, never hardcoded
                self.assertEqual(totals["calls"], backend.calls)
                self.assertEqual(totals["calls"], summary["jobs_done"])
                self.assertEqual(totals["calls"], rec["jobs_done"])
                self.assertEqual(totals["calls"], len(rec["completed"]))

                # actual grid matches the plan row by row
                self.assertEqual(totals["rounds"], len(state["rounds"]))
                self.assertEqual(len(rows), len(state["rounds"]))
                for row, rd in zip(rows, state["rounds"]):
                    self.assertEqual(row["alive"], len(rd["alive"]))
                    self.assertEqual(row["matches"], len(rd["matches"]))
                    self.assertEqual(bool(row["bye"]), rd["bye"] is not None)

                # every participant has a non-empty spawn solution on disk
                for aid, a in state["agents"].items():
                    path = entry.path(a["solution"])
                    self.assertTrue(os.path.isfile(path) and os.path.getsize(path) > 0,
                                    "missing solution for %s" % aid)

    def test_n1_degenerate_run(self):
        """N=1 (verified against the code, not guessed): champion is crowned
        at new_state, run executes exactly the 1 spawn call, no rounds, no
        final, status done."""
        entry = _ladder(1)
        state, summary = entry.state, entry.summary
        rec = state[orch.ORCHESTRATION_KEY]
        self.assertEqual(orch.STATUS_DONE, rec["status"])
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertTrue(summary["finished"])
        self.assertEqual("a001", state["champion"])
        self.assertEqual([], state["rounds"])
        self.assertIsNone(state["final"])
        self.assertEqual(1, core.plan_totals(1, WAVE)["calls"])
        self.assertEqual(1, entry.backend.calls)
        self.assertEqual(1, rec["jobs_done"])
        self.assertEqual(1, len(rec["completed"]))
        self.assertIn("a001", next(iter(rec["completed"])))
        self.assertEqual([], summary["failed"])

    def test_same_seed_deterministic(self):
        """Same seed -> identical champion, identical match winners,
        identical call count, even across different run directories."""
        first = _probe(7, "det-seed")
        second = _probe(7, "det-seed")
        self.assertEqual(first["champion"], second["champion"])
        self.assertEqual(first["winners"], second["winners"])
        self.assertEqual(first["calls"], second["calls"])
        self.assertEqual(core.plan_totals(7, WAVE)["calls"], first["calls"])


# ---------------------------------------------------------------------------
# Waves vs plan (wave_batches / effective_wave, unit level)
# ---------------------------------------------------------------------------


class WavesPlanTest(TmpCwdTestCase):
    """Wave chunking agrees with the plan's per-round wave counts."""

    def test_wave_batches_match_plan_waves(self):
        state = self.fresh_state(16, "waves")
        spawn = core.phase_jobs(state, "spawn")
        attack = core.phase_jobs(state, "attack")
        defend = core.phase_jobs(state, "defend")
        judge = core.phase_jobs(state, "judge")
        self.assertEqual(16, len(spawn))
        self.assertEqual(16, len(attack))   # 2 per match x 8 matches
        self.assertEqual(16, len(defend))
        self.assertEqual(8, len(judge))
        self.assertEqual(2, len(orch.wave_batches(spawn, WAVE)))
        self.assertEqual(2, len(orch.wave_batches(attack, WAVE)))
        self.assertEqual(2, len(orch.wave_batches(defend, WAVE)))
        self.assertEqual(1, len(orch.wave_batches(judge, WAVE)))
        rows = core.plan_rows(16, WAVE)
        self.assertEqual(rows[0]["waves"], 2 + 2 + 1)
        totals = core.plan_totals(16, WAVE)
        self.assertEqual(totals["waves"],
                         len(orch.wave_batches(spawn, WAVE))
                         + sum(r["waves"] for r in rows))

    def test_wave_guards(self):
        state = self.fresh_state(2, "waveguards")
        self.assertEqual(WAVE, orch.effective_wave(state))
        no_wave = dict(state)
        no_wave.pop("wave")
        self.assertEqual(core.DEFAULT_WAVE, orch.effective_wave(no_wave))
        for bad in (0, -3, True, "x"):
            with self.subTest(wave=bad):
                with self.assertRaises(core.ArenaError):
                    orch.effective_wave(dict(state, wave=bad))
        self.assertEqual([[1, 2, 3], [4]], orch.wave_batches([1, 2, 3, 4], 3))
        self.assertEqual([], orch.wave_batches([], 5))
        with self.assertRaises(core.ArenaError):
            orch.wave_batches([1], 0)


# ---------------------------------------------------------------------------
# (c) Failure recovery
# ---------------------------------------------------------------------------


class FailureRecoveryTest(TmpCwdTestCase):
    """Timeout / malformed verdict / provider error / cancel / restart."""

    def test_transport_timeout_retries_pauses_and_resume_finishes(self):
        state = self.fresh_state(7, "flaky")

        def on_attempt(job, call_no):
            if job["kind"] == "attacker":
                raise orch.JobTransportError("simulated transport timeout")
            return None

        backend = FakeArenaBackend(on_attempt=on_attempt)
        summary = orch.run(state, backend)
        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        rec = state[orch.ORCHESTRATION_KEY]
        self.assertEqual(1, len(rec["failures"]))
        job_id = next(iter(rec["failures"]))
        failure = rec["failures"][job_id]
        self.assertEqual("transport", failure["class"])
        self.assertEqual(orch.MAX_RETRIES + 1, failure["attempts"])
        self.assertEqual({"transport": orch.MAX_RETRIES, "schema": 0,
                          "attempts": orch.MAX_RETRIES + 1},
                         rec["retries"][job_id])
        self.assertTrue(rec["last_error"].startswith(job_id))
        # the 7 spawns survived; the flaky attack was attempted exactly 3 times
        self.assertEqual(7, len(rec["completed"]))
        self.assertEqual(7, rec["jobs_done"])
        self.assertEqual(orch.MAX_RETRIES + 1, backend.attempted(job_id))

        # reload from disk (crash/pause independence), resume, finish
        state2 = core.load_state(os.path.join("runs", "flaky"))
        self.assertEqual(orch.STATUS_PAUSED, orch.status_of(state2))
        orch.resume(state2)
        healthy = FakeArenaBackend()
        summary2 = orch.run(state2, healthy)
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        totals = core.plan_totals(7, WAVE)
        self.assertEqual(totals["calls"] - 7, summary2["jobs_done"])
        self.assertEqual(1, healthy.attempted(job_id))
        for done_id in rec["completed"]:
            self.assertEqual(0, healthy.attempted(done_id))
        self.assertTrue(state2["champion"])

    def test_undecidable_judge_verdict_is_schema_error_and_pauses(self):
        state = self.fresh_state(2, "hostileverdict")

        def on_attempt(job, call_no):
            if job["kind"] == "judge":
                # structurally valid JUDGE payload, but decides nobody:
                # must become a retryable schema error, never reach disk
                return {"winner": "a999; DROP TABLE agents", "reason": "hostile",
                        "survived": [], "scores": {}}
            return None

        backend = FakeArenaBackend(on_attempt=on_attempt)
        summary = orch.run(state, backend)
        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        judge_job = core.phase_jobs(state, "judge")[0]
        rec = state[orch.ORCHESTRATION_KEY]
        failure = rec["failures"][judge_job["id"]]
        self.assertEqual("schema", failure["class"])
        self.assertEqual(orch.MAX_RETRIES + 1, failure["attempts"])
        self.assertIn("not decidable", failure["error"])
        self.assertEqual({"transport": 0, "schema": orch.MAX_RETRIES,
                          "attempts": orch.MAX_RETRIES + 1},
                         rec["retries"][judge_job["id"]])
        self.assertEqual(6, rec["jobs_done"])          # spawn, attack, defend
        self.assertEqual(6, len(rec["completed"]))
        self.assertEqual(orch.MAX_RETRIES + 1, backend.attempted(judge_job["id"]))
        self.assertFalse(os.path.exists(judge_job["outputs"][0]))
        match = core.current_round(state)["matches"][0]
        self.assertIsNone(match["winner"])             # no garbage result

        orch.resume(state)
        summary2 = orch.run(state, FakeArenaBackend())
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertIn(state["champion"], state["agents"])

    def test_non_object_payload_is_schema_error(self):
        state = self.fresh_state(2, "stroper")

        class _StringBackend(object):
            def __call__(self, envelope):
                return "I am not a JSON object"

        job = core.phase_jobs(state, "spawn")[0]
        with self.assertRaises(orch.JobSchemaError) as ctx:
            orch.execute_job(state, job, _StringBackend())
        self.assertIn("expected a JSON object", str(ctx.exception))
        missing = [j["id"] for j in core.missing_jobs(core.phase_jobs(state, "spawn"))]
        self.assertIn(job["id"], missing)

    def test_idempotency_key_contract_and_echo_mismatch(self):
        state = self.fresh_state(2, "idem")
        jobs = core.phase_jobs(state, "spawn")
        first, second = jobs[0], jobs[1]
        key1 = orch.idempotency_key(state, first)
        key2 = orch.idempotency_key(state, second)
        # format: 32 hex chars, distinct per job, sensitive to output names
        self.assertEqual(32, len(key1))
        int(key1, 16)
        self.assertNotEqual(key1, key2)
        mutated = dict(first)
        mutated["outputs"] = ["totally-other-name.md"]
        self.assertNotEqual(key1, orch.idempotency_key(state, mutated))
        # deterministic across a save/reload cycle (machine-independent)
        core.save_state(state)
        reloaded = core.load_state(os.path.join("runs", "idem"))
        self.assertEqual(key1, orch.idempotency_key(reloaded, first))
        # happy path: correct echo accepted, outputs written, job done
        info = orch.execute_job(state, first, FakeArenaBackend())
        self.assertEqual(key1, info["idempotency_key"])
        self.assertNotIn(first["id"],
                         [j["id"] for j in core.missing_jobs(core.phase_jobs(state, "spawn"))])
        # wrong echo -> JobSchemaError, no output written
        wrong = FakeArenaBackend(
            on_attempt=lambda job, call: {"solution": "s", "idempotency_key": "0" * 32})
        with self.assertRaises(orch.JobSchemaError) as ctx:
            orch.execute_job(state, second, wrong)
        self.assertIn("mismatch", str(ctx.exception))
        self.assertFalse(os.path.exists(second["outputs"][0]))
        # absent echo is allowed
        silent = FakeArenaBackend(on_attempt=lambda job, call: {"solution": "s"})
        info2 = orch.execute_job(state, second, silent)
        self.assertEqual(key2, info2["idempotency_key"])

    def test_provider_error_fails_fast_without_retry(self):
        state = self.fresh_state(4, "provider")

        def on_attempt(job, call_no):
            if job["kind"] == "defender":
                raise RuntimeError("provider exploded (non-retryable)")
            return None

        backend = FakeArenaBackend(on_attempt=on_attempt)
        summary = orch.run(state, backend)
        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        rec = state[orch.ORCHESTRATION_KEY]
        self.assertEqual(1, len(rec["failures"]))
        job_id = next(iter(rec["failures"]))
        failure = rec["failures"][job_id]
        self.assertEqual("backend", failure["class"])
        self.assertEqual(1, failure["attempts"])
        self.assertNotIn(job_id, rec["retries"])       # no retry bookkeeping
        self.assertEqual(1, backend.attempted(job_id))
        # spawns + all attacks survived the hard failure
        self.assertEqual(4 + 4, rec["jobs_done"])

        orch.resume(state)
        healthy = FakeArenaBackend()
        summary2 = orch.run(state, healthy)
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertEqual(core.plan_totals(4, WAVE)["calls"] - 8, summary2["jobs_done"])
        self.assertEqual(1, healthy.attempted(job_id))
        self.assertTrue(state["champion"])

    def test_cancel_mid_run_is_terminal_and_irreversible(self):
        state = self.fresh_state(4, "cancels5")
        spawn_jobs = core.phase_jobs(state, "spawn")
        backend = FakeArenaBackend()

        def on_call(envelope):
            if backend.calls == 3:
                orch.cancel(state)  # cooperative cancel at a job boundary

        backend.on_call = on_call
        summary = orch.run(state, backend)
        self.assertEqual(orch.STATUS_CANCELLED, summary["status"])
        rec = state[orch.ORCHESTRATION_KEY]
        self.assertEqual(3, rec["jobs_done"])          # in-flight job finished
        # the 3 completed spawns are on disk, the 4th never ran
        for job in spawn_jobs[:3]:
            self.assertTrue(os.path.exists(job["outputs"][0]), job["id"])
        for job in spawn_jobs[3:]:
            self.assertFalse(os.path.exists(job["outputs"][0]), job["id"])

        # cancelled survives a reload and is terminal for every entry point
        state2 = core.load_state(os.path.join("runs", "cancels5"))
        self.assertEqual(orch.STATUS_CANCELLED, orch.status_of(state2))
        healthy = FakeArenaBackend()
        with self.assertRaises(core.ArenaError):
            orch.run(state2, healthy)
        with self.assertRaises(core.ArenaError):
            orch.resume(state2)
        with self.assertRaises(core.ArenaError):
            orch.pause(state2)
        self.assertEqual(0, healthy.calls)             # nothing re-executed
        # repeated cancel is an idempotent no-op that keeps completed work
        rec2 = orch.cancel(state2)
        self.assertEqual(orch.STATUS_CANCELLED, rec2["status"])
        self.assertEqual(3, rec2["jobs_done"])

    def test_crash_keyboard_interrupt_restart_finishes_without_reexecution(self):
        state = self.fresh_state(4, "crashs5")
        spawn_jobs = core.phase_jobs(state, "spawn")
        backend = FakeArenaBackend()

        def on_call(envelope):
            if backend.calls == 3:
                raise KeyboardInterrupt("simulated crash between jobs")

        backend.on_call = on_call
        with self.assertRaises(KeyboardInterrupt):
            orch.run(state, backend)
        self.assertEqual(2, len(backend.attempts))     # crash is not retried
        # state stays running on disk; completed outputs are intact
        state2 = core.load_state(os.path.join("runs", "crashs5"))
        self.assertEqual(orch.STATUS_RUNNING, orch.status_of(state2))
        snapshots = {}
        for job in spawn_jobs[:2]:
            with open(job["outputs"][0], "rb") as fh:
                snapshots[job["id"]] = fh.read()
        # resume is an idempotent no-op for a crashed (running) orchestration
        orch.resume(state2)
        self.assertEqual(orch.STATUS_RUNNING, orch.status_of(state2))
        healthy = FakeArenaBackend()
        summary = orch.run(state2, healthy)
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertEqual(core.plan_totals(4, WAVE)["calls"] - 2, summary["jobs_done"])
        # pre-crash work was never re-executed and never changed
        for job_id, blob in snapshots.items():
            self.assertEqual(0, healthy.attempted(job_id))
        for job in spawn_jobs[:2]:
            with open(job["outputs"][0], "rb") as fh:
                self.assertEqual(snapshots[job["id"]], fh.read())
        self.assertTrue(state2["champion"])


# ---------------------------------------------------------------------------
# (d) Security
# ---------------------------------------------------------------------------


class SecurityTest(TmpCwdTestCase):
    """Path traversal, oversized task, malicious payloads, tampering."""

    def test_run_id_traversal_rejected_and_contained(self):
        res = tools.arena_start({"task": "survive traversal probes",
                                 "agents": 2, "seed": "s5sec", "dir": "runs"})
        self.assertTrue(res.get("ok"), res)
        runs_before = {entry for entry in os.listdir("runs") if entry != core.LATEST}
        self.assertEqual(1, len(runs_before))
        os.makedirs("outside", exist_ok=True)
        marker = os.path.join("outside", "keep.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("do not delete")
        for bad in ("..", "../outside", "sub/../../outside"):
            with self.subTest(run=bad):
                st = tools.arena_status({"run": bad, "dir": "runs"})
                self.assertFalse(st.get("ok"))
                self.assertIn("escapes", st.get("error", ""))
                rs = tools.arena_result({"run": bad, "dir": "runs"})
                self.assertFalse(rs.get("ok"))
        # cancel with purge must never delete anything outside the base dir
        cc = tools.arena_cancel({"run": "../outside", "dir": "runs", "purge": True})
        self.assertFalse(cc.get("ok"))
        self.assertTrue(os.path.isfile(marker))
        self.assertEqual(runs_before, {entry for entry in os.listdir("runs") if entry != core.LATEST})
        self.assertEqual({"runs", "outside"}, set(os.listdir(".")))
        # a legitimate run is still reachable afterwards (default = LATEST)
        st = tools.arena_status({"dir": "runs"})
        self.assertTrue(st.get("ok"), st)

    def test_oversized_task_has_no_schema_cap_and_stays_inside(self):
        big = "T" * 1000000 + "END-MARKER"
        # the schema declares no maxLength: structural validation accepts it
        self.assertTrue(schemas.validate_tool_input(
            "arena_start", {"task": big, "agents": 2, "seed": "big"}))
        task_spec = schemas.START_SCHEMA["properties"]["task"]
        self.assertNotIn("maxLength", task_spec)
        state = self.fresh_state(2, "big")
        state["task"] = big
        prompt_path = core.phase_jobs(state, "spawn")[0]["prompt"]
        summary = orch.run(state, FakeArenaBackend())
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        # round-trips verbatim: no truncation, no escape outside the run
        loaded = core.load_state(os.path.join("runs", "big"))
        self.assertEqual(big, loaded["task"])
        with open(prompt_path, encoding="utf-8") as fh:
            self.assertIn(big, fh.read())
        for dirpath, _dirnames, filenames in os.walk("."):
            for name in filenames:
                self.assertNotIn("..", os.path.normpath(
                    os.path.join(dirpath, name)).split(os.sep))

    def test_malicious_participant_output_contained_and_verbatim(self):
        state = self.fresh_state(2, "evil")
        evil = ("x\"; rm -rf /\n" + "../" * 40 + "escape.md\n"
                + "<script>alert(1)</script>\n"
                + json.dumps({"winner": "a002"}) + "\n"
                + "üñïçø∂é 🙂\n" + "E" * 50000)

        def on_attempt(job, call_no):
            if job["kind"] == "competitor" and job["agent"] == "a001":
                return {"solution": evil}
            if job["kind"] == "defender":
                return {"defense": evil, "revised_solution": evil}
            return None

        verdict_path = core.phase_jobs(state, "judge")[0]["outputs"][0]
        backend = FakeArenaBackend(on_attempt=on_attempt)
        summary = orch.run(state, backend)
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertIn(state["champion"], state["agents"])
        # the hostile text was stored verbatim at the engine-chosen path
        with open(os.path.join(state["dir"], state["agents"]["a001"]["solution"]), encoding="utf-8") as fh:
            self.assertEqual(evil + "\n", fh.read())
        # the verdict file is engine-written JSON that decides the match
        with open(verdict_path, encoding="utf-8") as fh:
            verdict = json.load(fh)
        self.assertEqual(state["champion"], verdict["winner"])
        # nothing escaped the run directory (payloads are data, never paths)
        for dirpath, dirnames, filenames in os.walk("."):
            for name in dirnames:
                self.assertFalse(os.path.islink(os.path.join(dirpath, name)))
            for name in filenames:
                self.assertNotIn("..", os.path.normpath(
                    os.path.join(dirpath, name)).split(os.sep))
        self.assertEqual({"runs"}, set(os.listdir(".")))

    def test_tampered_verdict_file_causes_controlled_pause(self):
        state = self.fresh_state(4, "tampered")
        judge_jobs = core.phase_jobs(state, "judge")
        target = judge_jobs[0]
        os.makedirs(os.path.dirname(target["outputs"][0]), exist_ok=True)
        with open(target["outputs"][0], "w", encoding="utf-8") as fh:
            fh.write("{not-json{{{")
        backend = FakeArenaBackend()
        summary = orch.run(state, backend)
        # controlled failure: paused with a precise message, no crash, and
        # the untouched match was still judged and recorded
        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        rec = state[orch.ORCHESTRATION_KEY]
        self.assertIn("still missing", rec["last_error"])
        self.assertIn(target["id"], rec["last_error"])
        self.assertFalse(os.path.exists(target["outputs"][0]))
        self.assertTrue(os.path.exists(target["outputs"][0] + ".unreadable"))
        rd = core.current_round(state)
        judged = sum(1 for m in rd["matches"] if m["winner"])
        self.assertEqual(1, judged)
        # recovery: resume re-runs only the missing judge and finishes
        orch.resume(state)
        summary2 = orch.run(state, FakeArenaBackend())
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertIn(state["champion"], state["agents"])

    def test_corrupt_state_file_detected_by_load_state(self):
        run = os.path.join("runs", "corrupt")
        state = self.fresh_state(2, "corrupt")
        core.save_state(state)
        path = os.path.join(run, "arena.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{this is not json")
        with self.assertRaises(ValueError):
            core.load_state(run)
        # truncated-but-valid prefix is detected the same way
        core.save_state(state)
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw[:len(raw) // 2])
        with self.assertRaises(ValueError):
            core.load_state(run)

    def test_orchestration_tampering_self_heals(self):
        state = self.fresh_state(2, "orchtamper")
        rec = orch.ensure_orchestration(state)
        rec["status"] = "pwned"
        # unknown status coerces back to idle; resume from idle refuses
        self.assertEqual(orch.STATUS_IDLE, orch.status_of(state))
        with self.assertRaises(core.ArenaError):
            orch.resume(state)
        # the run itself heals: garbage status is replaced, run completes
        summary = orch.run(state, FakeArenaBackend())
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertIn(state["champion"], state["agents"])
        # a completely missing record is recreated with safe defaults
        del state[orch.ORCHESTRATION_KEY]
        rec2 = orch.ensure_orchestration(state)
        self.assertEqual(orch.STATUS_IDLE, rec2["status"])
        self.assertEqual(1, rec2["schema_version"])
        self.assertEqual({}, rec2["completed"])
        self.assertEqual({}, rec2["retries"])
        self.assertEqual({}, rec2["failures"])
        self.assertEqual(0, rec2["jobs_done"])
        self.assertIsNone(rec2["last_error"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

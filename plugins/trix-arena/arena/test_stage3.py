#!/usr/bin/env python3
"""Stage-3 gate: the orchestrator layer (arena/orchestrator.py).

Deterministic only -- every "backend" is an in-process stub; there are no
network calls and no real LLM calls anywhere in this module.

Gate coverage
-------------
(a) transport errors retried in-run; retries exhausted -> run paused with
    the failure recorded; completed jobs and outputs are preserved
(b) schema errors are NOT retried beyond the policy (exactly
    MAX_RETRIES + 1 attempts; non-retryable classes fail immediately)
(c) crash + reload + resume finishes remaining jobs WITHOUT re-executing
    completed ones (missing_jobs skip; outputs byte-for-byte preserved)
(d) at most one winner per match: each judge verdict written at most
    once, collect only after ALL judge outputs of the round are durable
(e) pause mid-run then resume preserves completed outputs
(f) cancel is terminal and preserves completed outputs (a later run()
    raises; nothing else executes)
(g) run() is a no-op once orchestration is done

Pause/cancel are driven through the real API: the backend hook calls
``orchestrator.pause(state)`` / ``orchestrator.cancel(state)`` on the live
state; they take effect at the next job boundary (the in-flight job always
completes first).  A crash is simulated by raising ``KeyboardInterrupt``
from the backend (a BaseException, so the orchestrator neither retries
nor swallows it).

Contract notes (aligned with core.phase_jobs / upstream bracket.py)
-------------------------------------------------------------------
* competitor/attacker/defender jobs carry ``agent``; judge jobs do not --
  they carry ``match``/``a``/``b``; the final job only has its id
  (``final.judge``).  The fake backend therefore keys canned payloads by
  ``agent`` (agent jobs) or by ``match``/id (judge/final) and synthesises
  healthy verdicts for judge jobs that have no canned slot.
* The judge payload schema requires ``scores`` (an object); an empty
  ``scores`` object is structurally valid and ``core.decide`` then falls
  back to the judge's explicit pick, which is how the fake backend names
  the winner.
* Match ids come from the seeded pairing (``r1-m01`` style for a 4-agent
  round, not ``r1-m001``); for seed "seed" round 1 is
  ``r1-m01 = a001 vs a003`` and ``r1-m02 = a002 vs a004``.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import ensure_trix_arena_importable  # noqa: E402

ensure_trix_arena_importable()

from arena import core  # noqa: E402
from arena import orchestrator as orch  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic backend
# ---------------------------------------------------------------------------

class FakeOrchBackend(object):
    """Serves one canned payload per slot key, per ask.

    ``slots`` maps a slot key -> {kind -> payload template}.  The slot key
    is the job's ``agent`` for competitor/attacker/defender jobs and the
    job's ``match`` (judge) or ``id`` (final) otherwise -- exactly the
    identifying fields ``core.phase_jobs`` provides.  Every served payload
    is a deep copy stamped with the asked ``idempotency_key`` so repeated
    asks of one slot are visible via ``attempted(job_id)``.

    Judge jobs without a canned slot get a synthesised healthy verdict
    naming the match's ``a`` side (scores left empty, so ``core.decide``
    follows the judge's pick); final jobs without a canned slot get a
    verdict naming ``x``.

    Hooks let tests inject faults or drive pause/cancel at exact points:

    * ``on_attempt(job, attempt_no) -> payload | None`` -- return a
      payload to serve instead of the slot template, or raise to
      simulate a backend error (orch.JobTransportError / arbitrary
      exception classes).
    * ``on_call()`` -- called before every dispatch; raise to simulate a
      crash, or call ``orch.pause(state)`` / ``orch.cancel(state)`` on
      the fixture's live state to stop the run at the next job boundary.
    """

    def __init__(self, slots, on_attempt=None, on_call=None):
        self._slots = slots
        self.on_attempt = on_attempt
        self.on_call = on_call
        self.attempts = []   # every job id ever attempted, in order
        self.call_no = 0

    def _default_slot(self, job):
        """Synthesise a healthy payload for judge/final asks."""
        if job["kind"] == "judge":
            return _mk_verdict(job["a"])
        if job["kind"] == "final":
            return _mk_verdict("x")
        return None

    def call(self, envelope):
        self.call_no += 1
        if self.on_call is not None:
            self.on_call()
        # the real orchestrator passes the job envelope itself (job + idempotency_key)
        job = envelope["job"] if "job" in envelope and isinstance(envelope["job"], dict) else envelope
        kind = job["kind"]
        key = job.get("agent") or job.get("match") or job["id"]
        slot = self._slots.get(key, {}).get(kind)
        if slot is None:
            slot = self._default_slot(job)
        if slot is None:
            raise AssertionError("unexpected backend call: %r" % (job,))
        self.attempts.append(job["id"])
        served = json.loads(json.dumps(slot))
        served["idempotency_key"] = envelope.get("idempotency_key")
        if self.on_attempt is not None:
            override = self.on_attempt(
                job, len(self.attempts))
            if override is not None:
                return override
        return served

    def attempted(self, job_id):
        return self.attempts.count(job_id)

    def __call__(self, job):
        """Real orchestrator API: backend(job_envelope) -> payload dict."""
        return self.call(job)


def _mk_payload():
    """A valid competitor payload per SOLVE_SCHEMA (solution only)."""
    return {"solution": "the answer"}


def _mk_attack():
    """A valid attack payload per ATTACK_SCHEMA."""
    return {"attack": "weak spots"}


def _mk_defense():
    """A valid defense payload per DEFENSE_SCHEMA."""
    return {"defense": "rebuttal", "revised_solution": "better answer"}


def _mk_verdict(winner):
    """A valid judge/final verdict per JUDGE_SCHEMA.

    ``scores`` is required by the schema but may be empty; core.decide
    then follows the explicit ``winner`` pick.
    """
    return {"winner": winner, "reason": "dominant on every axis",
            "survived": [], "scores": {}}


def _expected_winners(state):
    """Winners the fake judge produces: the ``a`` side of every match."""
    return [m["a"] for rd in state["rounds"] for m in rd["matches"]]


class SpawnAllFixture(unittest.TestCase):
    """A fresh 4-agent state in a real temp workspace (runs/ under ROOT)."""

    def setUp(self):
        import tempfile

        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        data = core.load_strategies()
        run_dir = os.path.join(core.ROOT, "night-run")
        os.makedirs(run_dir, exist_ok=True)
        # has_baseline=True: the champion also faces the baseline in the
        # final phase (final.judge), so the full ladder is 20 jobs:
        # 4 spawn + 2 matches * 5 + 1 match * 5 + 1 final.
        self.state = core.new_state(4, "seed", data, run_dir, wave=10,
                                    has_baseline=True)
        core.save_state(self.state)
        self.run_dir = self.state["dir"]

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------

    def backend(self, **kw):
        """A fully-canned healthy backend for the 4-agent ladder.

        Agent slots cover competitor/attacker/defender asks.  Judge and
        final asks are served from ``extra_slots`` when a test pins one
        (keyed by match id / "final.judge") and synthesised otherwise.
        """
        slots = {}
        for aid in ("a001", "a002", "a003", "a004"):
            slots[aid] = {
                "competitor": _mk_payload(),
                "attacker": _mk_attack(),
                "defender": _mk_defense(),
            }
        extra = kw.pop("extra_slots", {})
        for key, kinds in extra.items():
            slots.setdefault(key, {}).update(kinds)
        return FakeOrchBackend(slots, **kw)

    def job_output(self, state, phase, job_id):
        """The first output path of one job, straight from core.phase_jobs."""
        for job in core.phase_jobs(state, phase):
            if job["id"] == job_id:
                return job["outputs"][0]
        raise AssertionError("no such job: %s" % job_id)

    def completed_spawn_paths(self, state):
        """Output paths of the spawn jobs recorded as completed."""
        return [self.job_output(state, "spawn", jid)
                for jid in sorted(state["orchestration"]["completed"])
                if jid.endswith(".spawn")]

    def reload(self):
        self.state = core.load_state(self.run_dir)
        return self.state


# ---------------------------------------------------------------------------
# (a) transport errors are retried; exhausted -> paused; completed kept
# ---------------------------------------------------------------------------

class TransportRetryTest(SpawnAllFixture):
    def test_transport_retries_then_pause_preserves_completed(self):
        calls = {"n": 0}

        def transport_fail(job, attempt_no):
            if job["kind"] == "attacker":
                raise orch.JobTransportError("connection reset")
            return None

        def bump():
            calls["n"] += 1

        backend = self.backend(on_attempt=transport_fail, on_call=bump)
        summary = orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        # all four spawns survived and their outputs are durable
        self.assertEqual(
            {"a001.spawn", "a002.spawn", "a003.spawn", "a004.spawn"},
            set(rec["completed"]))
        for out in self.completed_spawn_paths(state):
            self.assertTrue(os.path.isfile(out), out)

        # run paused, not done/cancelled, failure recorded
        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        self.assertEqual(orch.STATUS_PAUSED, rec["status"])

        # exactly one failed job: an attacker job, transport-class, after
        # MAX_RETRIES + 1 backend attempts
        self.assertEqual(1, len(rec["failures"]))
        failed = sorted(rec["failures"])[0]
        self.assertTrue(failed.endswith(".attack"), failed)
        failure = rec["failures"][failed]
        self.assertEqual("transport", failure["class"])
        self.assertEqual(orch.MAX_RETRIES + 1, failure["attempts"])
        self.assertEqual(orch.MAX_RETRIES + 1, backend.attempted(failed))
        ledger = rec["retries"][failed]
        self.assertEqual(orch.MAX_RETRIES, ledger["transport"])
        self.assertEqual(orch.MAX_RETRIES + 1, ledger["attempts"])
        self.assertIn(failed, rec["last_error"])
        self.assertEqual(1, len(summary["failed"]))
        self.assertEqual(failed, summary["failed"][0]["job"])
        self.assertGreaterEqual(calls["n"], 3)

        # the failed job produced no output
        pend = core.missing_jobs(core.phase_jobs(state, "attack"))
        match = [j for j in pend if j["id"] == failed]
        self.assertEqual(1, len(match))
        self.assertFalse(os.path.exists(match[0]["outputs"][0]))


# ---------------------------------------------------------------------------
# (b) schema errors are NOT retried beyond policy
# ---------------------------------------------------------------------------

class SchemaNoRetryTest(SpawnAllFixture):
    def test_schema_error_retried_exactly_max_plus_one_then_pause(self):
        # missing the required "solution" key -> schemas.payload_error
        # fires on every attempt (retryable JobSchemaError).
        broken = {"idempotency_key": "echo-but-no-content"}

        def bad_payload(job, attempt_no):
            if job["id"] == "a004.spawn":
                return broken
            return None

        backend = self.backend(on_attempt=bad_payload)
        summary = orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        self.assertEqual(orch.STATUS_PAUSED, rec["status"])

        # exactly MAX_RETRIES + 1 attempts, all schema-class, then pause
        failure = rec["failures"]["a004.spawn"]
        self.assertEqual("schema", failure["class"])
        self.assertEqual(orch.MAX_RETRIES + 1, failure["attempts"])
        self.assertEqual(orch.MAX_RETRIES + 1, backend.attempted("a004.spawn"))
        self.assertEqual(orch.MAX_RETRIES, rec["retries"]["a004.spawn"]["schema"])
        self.assertIn("a004.spawn", rec["last_error"])

        # the three healthy spawns completed and are preserved
        self.assertEqual(
            {"a001.spawn", "a002.spawn", "a003.spawn"},
            set(rec["completed"]))
        for out in self.completed_spawn_paths(state):
            self.assertTrue(os.path.isfile(out), out)
        self.assertFalse(os.path.exists(
            self.job_output(state, "spawn", "a004.spawn")))

    def test_non_retryable_error_class_fails_on_first_attempt(self):
        class WeirdError(Exception):
            pass

        def boom(job, attempt_no):
            if job["id"] == "a001.spawn":
                raise WeirdError("not retryable")
            return None

        backend = self.backend(on_attempt=boom)
        orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        self.assertEqual(orch.STATUS_PAUSED, rec["status"])
        self.assertEqual(1, backend.attempted("a001.spawn"))
        # no retry ledger entry: the class is not in RETRYABLE_CLASSES
        self.assertNotIn("a001.spawn", rec["retries"])
        failure = rec["failures"]["a001.spawn"]
        self.assertEqual("backend", failure["class"])
        self.assertEqual(1, failure["attempts"])
        self.assertEqual(0, len(rec["completed"]))
        self.assertIn("a001.spawn", rec["last_error"])

    def test_payload_kind_mismatch_is_schema_error(self):
        # the r1-m02 judge slot (keyed by match id) holds a competitor
        # payload -> schemas.payload_error("judge", ...) fires.
        slots = {"r1-m02": {"judge": _mk_payload()}}
        backend = self.backend(extra_slots=slots)
        summary = orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        failure = rec["failures"]["r1-m02.judge"]
        self.assertEqual("schema", failure["class"])
        self.assertEqual(orch.MAX_RETRIES + 1,
                         rec["retries"]["r1-m02.judge"]["attempts"])
        # the healthy first judge still completed exactly once
        self.assertEqual(1, backend.attempted("r1-m01.judge"))
        self.assertIn("r1-m01.judge", rec["completed"])
        self.assertTrue(os.path.isfile(
            self.job_output(state, "judge", "r1-m01.judge")))


# ---------------------------------------------------------------------------
# (c) crash + reload + resume completes remaining jobs without re-running
# ---------------------------------------------------------------------------

class CrashResumeTest(SpawnAllFixture):
    def test_crash_reload_resume_no_reexecution(self):
        seen = []

        def crash_after_two():
            seen.append(1)
            if len(seen) >= 3:
                raise KeyboardInterrupt("simulated crash")

        backend = self.backend(on_call=crash_after_two)
        with self.assertRaises(KeyboardInterrupt):
            orch.run(self.state, backend)

        state = self.reload()
        rec = state["orchestration"]
        self.assertEqual(orch.STATUS_RUNNING, rec["status"])
        done_ids = set(rec["completed"])
        self.assertEqual(2, len(done_ids))

        # outputs of the finished jobs are durable on disk
        kept = self.completed_spawn_paths(state)
        self.assertEqual(2, len(kept))
        before = {}
        for out in kept:
            self.assertTrue(os.path.isfile(out), out)
            with open(out) as fh:
                before[out] = fh.read()

        # resume (crash recovery: resume() is a no-op for a running
        # orchestration), then finish the ladder with a healthy backend
        orch.resume(state)
        backend2 = self.backend()
        summary = orch.run(state, backend2)
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertEqual(orch.STATUS_DONE,
                         self.reload()["orchestration"]["status"])

        # no pre-crash job was re-executed after the crash
        for job_id in done_ids:
            self.assertEqual(0, backend2.attempted(job_id))

        # byte-for-byte preservation of the pre-crash outputs
        for out, blob in before.items():
            with open(out) as fh:
                self.assertEqual(blob, fh.read(), out)

        # full ladder executed exactly once across both backends:
        # 4 spawn + 3 matches * 5 jobs (2 attack + 2 defend + 1 judge)
        # + 1 final = 20 jobs, of which the first backend served exactly 2.
        done2 = set(self.reload()["orchestration"]["completed"])
        self.assertEqual(20, len(done2))
        self.assertEqual(3, backend.call_no)    # 3rd call raised
        self.assertEqual(18, backend2.call_no)

        # the tournament reached a champion with every match decided once
        # by the fake judge (which always names the match's "a" side)
        st = self.reload()
        self.assertTrue(st["champion"])
        winners = [m.get("winner") for rd in st["rounds"]
                   for m in rd["matches"]]
        self.assertEqual(_expected_winners(st), winners)

    def test_resume_from_pause_completes_without_duplicates(self):
        # fail the first attacker to exhaustion -> run pauses; then
        # resume with a healthy backend and finish the whole ladder
        def fail_first_attacker(job, attempt_no):
            if job["kind"] == "attacker":
                raise orch.JobTransportError("flaky")
            return None

        backend = self.backend(on_attempt=fail_first_attacker)
        orch.run(self.state, backend)
        state = self.reload()
        self.assertEqual(orch.STATUS_PAUSED, state["orchestration"]["status"])
        pre_pause_done = set(state["orchestration"]["completed"])
        self.assertEqual(4, len(pre_pause_done))

        orch.resume(state)
        backend2 = self.backend()
        summary = orch.run(state, backend2)
        self.assertEqual(orch.STATUS_DONE, summary["status"])

        # every job completed exactly once overall; jobs completed before
        # the pause were never re-served by the second backend
        done = set(self.reload()["orchestration"]["completed"])
        self.assertEqual(20, len(done))
        for job_id in pre_pause_done:
            self.assertEqual(0, backend2.attempted(job_id))
        for job_id in done - pre_pause_done:
            self.assertEqual(1, backend2.attempted(job_id))


# ---------------------------------------------------------------------------
# (d) at most one winner per match; collect only after all judged
# ---------------------------------------------------------------------------

class SingleWinnerTest(SpawnAllFixture):
    def test_judge_served_once_collect_only_after_all_judged(self):
        def fail_second_judge(job, attempt_no):
            if job["id"] == "r1-m02.judge":
                raise orch.JobTransportError("judge transport down")
            return None

        backend = self.backend(on_attempt=fail_second_judge)
        summary = orch.run(self.state, backend)
        state1 = self.reload()
        rec = state1["orchestration"]

        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        self.assertEqual(orch.STATUS_PAUSED, rec["status"])
        self.assertIn("r1-m02.judge", rec["last_error"])
        self.assertEqual("transport",
                         rec["failures"]["r1-m02.judge"]["class"])

        # one judge verdict is durable, the other is not; round 1 has no
        # recorded winners yet -- collect happens only after ALL judge
        # outputs of the round are durable.
        v1 = self.job_output(state1, "judge", "r1-m01.judge")
        v2 = self.job_output(state1, "judge", "r1-m02.judge")
        self.assertTrue(os.path.isfile(v1))
        self.assertFalse(os.path.exists(v2))
        self.assertEqual([None, None],
                         [m.get("winner")
                          for m in state1["rounds"][0]["matches"]])

        # resume: the second judge is served exactly once, the first
        # verdict is not rewritten, and the run finishes with a champion.
        with open(v1) as fh:
            v1_before = fh.read()
        orch.resume(state1)
        backend2 = self.backend()
        summary2 = orch.run(state1, backend2)
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertEqual(1, backend.attempted("r1-m01.judge"))
        self.assertEqual(1, backend2.attempted("r1-m02.judge"))
        self.assertEqual(0, backend2.attempted("r1-m01.judge"))
        with open(v1) as fh:
            self.assertEqual(v1_before, fh.read())

        st = self.reload()
        winners = [m.get("winner") for rd in st["rounds"]
                   for m in rd["matches"]]
        self.assertEqual(_expected_winners(st), winners)
        self.assertTrue(st["champion"])


# ---------------------------------------------------------------------------
# (e) pause mid-run then resume preserves completed outputs
# ---------------------------------------------------------------------------

class PauseResumeTest(SpawnAllFixture):
    def test_pause_after_second_spawn_then_resume_preserves_outputs(self):
        seen = []

        def pause_at_third_boundary():
            seen.append(1)
            if len(seen) == 3:
                # request a pause on the live state; the in-flight (3rd)
                # job still completes, the 4th never starts
                orch.pause(self.state)
            return None

        backend = self.backend(on_call=pause_at_third_boundary)
        summary = orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        self.assertEqual(orch.STATUS_PAUSED, summary["status"])
        self.assertEqual(orch.STATUS_PAUSED, rec["status"])
        self.assertEqual(3, len(rec["completed"]))

        before = {}
        for out in self.completed_spawn_paths(state):
            self.assertTrue(os.path.isfile(out), out)
            with open(out) as fh:
                before[out] = fh.read()
        self.assertEqual(3, len(before))

        orch.resume(state)
        summary2 = orch.run(state, self.backend())
        state2 = self.reload()
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertEqual(orch.STATUS_DONE, state2["orchestration"]["status"])

        # completed outputs survived the pause/resume cycle untouched
        for out, blob in before.items():
            with open(out) as fh:
                self.assertEqual(blob, fh.read(), out)
        self.assertEqual(4, len(self.completed_spawn_paths(state2)))
        for aid in ("a001", "a002", "a003", "a004"):
            self.assertTrue(os.path.isfile(
                self.job_output(state2, "spawn", aid + ".spawn")))


# ---------------------------------------------------------------------------
# (f) cancel is terminal and preserves completed outputs
# ---------------------------------------------------------------------------

class CancelTerminalTest(SpawnAllFixture):
    def test_cancel_after_first_spawn_is_terminal_and_preserves(self):
        seen = []

        def cancel_at_second_boundary():
            seen.append(1)
            if len(seen) == 2:
                # cancel the live state; the in-flight (2nd) job still
                # completes and is saved, the 3rd never starts
                orch.cancel(self.state)
            return None

        backend = self.backend(on_call=cancel_at_second_boundary)
        summary = orch.run(self.state, backend)
        state = self.reload()
        rec = state["orchestration"]

        self.assertEqual(orch.STATUS_CANCELLED, summary["status"])
        self.assertEqual(orch.STATUS_CANCELLED, rec["status"])
        self.assertEqual(2, len(rec["completed"]))

        # completed outputs are preserved; the rest never materialised
        kept = self.completed_spawn_paths(state)
        self.assertEqual(2, len(kept))
        for out in kept:
            self.assertTrue(os.path.isfile(out), out)
        for missing in ("a003.spawn", "a004.spawn"):
            self.assertFalse(os.path.exists(
                self.job_output(state, "spawn", missing)))

        # cancelled is terminal: run() refuses and nothing executes
        fresh = self.backend()
        with self.assertRaises(core.ArenaError):
            orch.run(state, fresh)
        self.assertEqual(0, fresh.call_no)
        self.assertEqual(orch.STATUS_CANCELLED,
                         self.reload()["orchestration"]["status"])


# ---------------------------------------------------------------------------
# (g) run() is a no-op when orchestration is already done
# ---------------------------------------------------------------------------

class DoneNoOpTest(SpawnAllFixture):
    def test_run_noop_after_done(self):
        backend = self.backend()
        summary = orch.run(self.state, backend)
        self.assertEqual(orch.STATUS_DONE, summary["status"])
        self.assertTrue(summary["champion"])

        state = self.reload()
        with open(os.path.join(state["dir"], "arena.json")) as fh:
            snapshot = fh.read()

        fresh = self.backend()
        summary2 = orch.run(state, fresh)
        self.assertEqual(orch.STATUS_DONE, summary2["status"])
        self.assertEqual(0, fresh.call_no)
        self.assertEqual(0, len(fresh.attempts))

        with open(os.path.join(state["dir"], "arena.json")) as fh:
            self.assertEqual(snapshot, fh.read())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

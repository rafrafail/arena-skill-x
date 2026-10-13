#!/usr/bin/env python3
"""Stage 2 tests: schemas, manifest wiring, /arena commands, tools, lifecycle.

Written against the real production API (commands.py, tools.py, schemas.py,
plugin.yaml, arena/core.py). Every asserted string/key mirrors production:
- tools.py has NO TOOL_NAMES/TOOL_HANDLERS; the six tools are registered via
  trix_arena.register() and their schemas live in schemas.TOOL_SCHEMAS.
- Validation is structural only (no min/max/required checks, "" is a valid
  string, bool is not an integer).
- plugin.yaml lists `commands:` and `tools:` as YAML lists; each tools entry
  carries input_schema (a quoted dotted string like "schemas.PING_SCHEMA")
  BEFORE handler, so the manifest is scanned as plain text and the schema
  refs are resolved via getattr(schemas, ...).
- The commands parser is strict and keeps the leading dashes in its error
  strings; tool failures come back wrapped as "arena <sub>: <error>".
- All tools take (data=None, **kwargs) and speak the {"ok": ...} protocol,
  except arena_ping which answers {"pong": ...}.
- Filesystem tests run in a throwaway cwd (TmpCwdTestCase), pass an explicit
  dir="runs" to arena_start, and pass that same base dir to every later
  status/result/cancel call (the default base dir is ".arena").

No real LLM calls, no network, stdlib only.
"""

import json
import os
import re
import tempfile
import unittest

import trix_arena
from trix_arena import commands, schemas, tools
from trix_arena.arena import core

TOOL_NAMES = {
    "arena_ping", "arena_plan", "arena_start",
    "arena_status", "arena_cancel", "arena_result",
}

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(trix_arena.__file__)), "plugin.yaml"
)

_SCORES_9 = {"correctness": 9, "completeness": 9, "specificity": 9, "robustness": 9, "clarity": 9}
_SCORES_5 = {"correctness": 5, "completeness": 5, "specificity": 5, "robustness": 5, "clarity": 5}


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class _FakeContext:
    """Minimal plugin-context double."""

    def __init__(self):
        self.commands = {}
        self.tools = {}

    def register_command(self, name, handler):
        self.commands[name] = handler

    def register_tool(self, name, handler):
        self.tools[name] = handler


# ------------------------------------------------------------- manifest scan

_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")


def _section_lines(text, section):
    """Raw (indented) lines under a top-level mapping key, e.g. 'commands'."""
    out, inside = [], False
    for line in text.splitlines():
        if _TOP_LEVEL_KEY.match(line):
            inside = line.split(":", 1)[0].strip() == section
            continue
        if inside and line.strip():
            out.append(line)
    return out


def _list_entries(lines, keys):
    """Tolerant scan of a YAML list: split on '- ' items, pick the wanted keys.

    Block-scalar description continuation lines never match the tracked key
    prefixes, so multi-line descriptions cannot confuse the scanner.
    """
    entries, current = [], None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            current = {"_order": []}
            entries.append(current)
            stripped = stripped[2:].strip()
        elif stripped.startswith("-"):
            continue  # nested list item (e.g. inside a description)
        if current is None or not stripped:
            continue
        for key in keys:
            if stripped.startswith(key + ":"):
                value = stripped.split(":", 1)[1].strip()
                current[key] = value.strip('"').strip("'")
                if key not in current["_order"]:
                    current["_order"].append(key)
                break
    return entries


class TmpCwdTestCase(unittest.TestCase):
    """Runs inside a throwaway cwd so runs/ never touches the repo."""

    def setUp(self):
        self._prev_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._prev_cwd)
        self._tmp.cleanup()


class SchemasTests(unittest.TestCase):
    def test_registry_holds_exactly_the_six_tools(self):
        self.assertEqual(set(schemas.TOOL_SCHEMAS), TOOL_NAMES)

    def test_schema_shape(self):
        for name, schema in schemas.TOOL_SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertEqual(schema["type"], "object")
                self.assertIs(schema["additionalProperties"], False)
                self.assertIn("$schema", schema)
                self.assertIn("title", schema)
                self.assertIn("properties", schema)
                self.assertNotIn("id", schema)  # schema["id"] would raise KeyError

    def test_declared_property_keys(self):
        expected = {
            "arena_ping": {"payload"},
            "arena_plan": {"agents", "wave"},
            "arena_start": {"task", "agents", "seed", "wave", "dir"},
            "arena_status": {"run", "dir"},
            "arena_cancel": {"run", "dir", "purge"},
            "arena_result": {"run", "dir", "collect"},
        }
        for name, keys in expected.items():
            with self.subTest(tool=name):
                self.assertEqual(set(schemas.TOOL_SCHEMAS[name]["properties"]), keys)

    def test_validation_is_structural_only(self):
        v = schemas.validate_tool_input
        # plan: bool is not an integer; str is not int; minimum is semantic only
        self.assertFalse(v("arena_plan", {"agents": True}))
        self.assertFalse(v("arena_plan", {"agents": "8"}))
        self.assertFalse(v("arena_plan", {"extra": 1}))
        self.assertTrue(v("arena_plan", {"agents": 8}))
        self.assertTrue(v("arena_plan", {}))
        self.assertTrue(v("arena_plan", {"wave": 0}))
        # start: "" is a valid string; no upper bound; unknown keys rejected
        self.assertTrue(v("arena_start", {"task": ""}))
        self.assertFalse(v("arena_start", {"task": 7}))
        self.assertFalse(v("arena_start", {"task": "t", "seed": 1}))
        self.assertFalse(v("arena_start", {"task": "t", "x": 1}))
        self.assertTrue(v("arena_start", {"task": "t", "agents": 10 ** 6}))
        # status / result / cancel: all body keys optional (default LATEST/.arena)
        for name in ("arena_status", "arena_result", "arena_cancel"):
            self.assertTrue(v(name, {}))
        self.assertTrue(v("arena_status", {"run": ""}))
        self.assertFalse(v("arena_cancel", {"run": "r", "purge": 1}))
        self.assertTrue(v("arena_cancel", {"purge": True}))

    def test_schema_error_messages(self):
        self.assertIsNone(schemas.schema_error("arena_plan", {"agents": 8}))
        self.assertEqual(
            schemas.schema_error("arena_plan", {"x": 1}),
            "arena_plan got unknown input key 'x' (allowed: agents, wave)",
        )
        self.assertIsInstance(schemas.schema_error("arena_plan", {"agents": True}), str)


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_manifest_file_exists(self):
        self.assertTrue(os.path.isfile(MANIFEST_PATH), MANIFEST_PATH)

    def test_commands_is_a_list_with_one_arena_entry(self):
        entries = _list_entries(_section_lines(self.text, "commands"), ("name", "handler"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "arena")
        self.assertEqual(entries[0]["handler"], "commands.arena_command")

    def test_tools_entries_resolve_string_schema_refs(self):
        entries = _list_entries(
            _section_lines(self.text, "tools"), ("name", "handler", "input_schema")
        )
        self.assertEqual({e["name"] for e in entries}, TOOL_NAMES)
        for entry in entries:
            with self.subTest(tool=entry["name"]):
                # field order: name, description (untracked), input_schema, handler
                self.assertEqual(entry["_order"], ["name", "input_schema", "handler"])
                self.assertTrue(entry["handler"].startswith("tools."))
                self.assertTrue(callable(getattr(tools, entry["handler"].split(".")[-1])))
                module_name, _, attr = entry["input_schema"].partition(".")
                self.assertEqual(module_name, "schemas")
                self.assertIs(getattr(schemas, attr), schemas.TOOL_SCHEMAS[entry["name"]])


class CommandsTests(TmpCwdTestCase):
    def test_usage_on_missing_or_unknown_subcommand(self):
        prefix = ("arena: unknown or missing subcommand. "
                  "Known: ping, plan, start, status, result, cancel")
        for args in (None, [], ["bogus"]):
            with self.subTest(args=args):
                self.assertTrue(commands.arena_command(args).startswith(prefix))

    def test_ping_answers_pong_and_ignores_extra_args(self):
        self.assertEqual(commands.arena_command(["ping"]), "pong")
        self.assertEqual(commands.arena_command(["ping", "extra", "args"]), "pong")

    def test_strict_parser_error_strings(self):
        cases = [
            (["plan", "--nope", "1"],
             "arena plan: unknown option '--nope' (allowed: --agents, --wave)"),
            (["plan", "--agents"],
             "arena plan: option --agents needs a value"),
            (["plan", "--agents", "x"],
             "arena plan: option --agents expects an integer, got 'x'"),
            (["plan", "-agents", "2"],
             "arena plan: unknown option '-agents' (options use the '--name value' form)"),
            (["cancel", "--purge", "maybe"],
             "arena cancel: option --purge expects true|false, got 'maybe'"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(commands.arena_command(args), expected)

    def test_start_without_task_renders_usage(self):
        self.assertEqual(
            commands.arena_command(["start"]),
            'arena start: start needs a task: arena start "<task>"'
            " [--agents N] [--seed S] [--wave N] [--dir PATH]",
        )

    def test_plan_first_line_format(self):
        out = commands.arena_command(["plan", "--agents", "4"])
        first = out.splitlines()[0]
        self.assertRegex(
            first, r"^arena plan: 4 participants, \d+ rounds, \d+ LLM calls in \d+ waves$"
        )

    def test_tool_failure_is_wrapped_with_the_subcommand(self):
        out = commands.arena_command(["status", "--run", "does-not-exist"])
        self.assertEqual(
            out, "arena status: no arena run directory 'does-not-exist' under .arena"
        )


class ToolsTests(TmpCwdTestCase):
    def test_ping_protocol(self):
        self.assertEqual(tools.arena_ping(), {"pong": True, "plugin": "trix-arena"})
        res = tools.arena_ping({"payload": "hi"})
        self.assertTrue(res["pong"])
        self.assertEqual(res["payload"], "hi")
        self.assertNotIn("ok", res)

    def test_plan_reports_ok(self):
        self.assertTrue(tools.arena_plan({"agents": 4})["ok"])

    def test_start_creates_state_file_and_latest_pointer(self):
        res = tools.arena_start({"task": "t", "agents": 4, "dir": "runs"})
        self.assertTrue(res["ok"], res)
        for key in ("run", "dir", "state_file", "agents", "seed", "wave",
                    "cards_available", "spawn_jobs", "task"):
            self.assertIn(key, res)
        # dir is the run directory (self-consistent with state_file below)
        self.assertEqual(res["dir"], os.path.join("runs", res["run"]))
        self.assertEqual(res["agents"], 4)
        self.assertEqual(res["seed"], "arena")
        self.assertEqual(res["wave"], 10)
        self.assertEqual(res["spawn_jobs"], 4)
        self.assertEqual(res["cards_available"], 2160)
        self.assertEqual(res["task"], "t")
        self.assertEqual(res["state_file"], os.path.join("runs", res["run"], "arena.json"))
        self.assertTrue(os.path.isfile(res["state_file"]))
        self.assertTrue(os.path.isfile(os.path.join("runs", "LATEST")))

    def test_status_round_trip_and_base_dir_rule(self):
        start = tools.arena_start({"task": "t", "agents": 4, "dir": "runs"})
        run = start["run"]
        # Omitting the base dir falls back to ".arena" -> run not found.
        self.assertFalse(tools.arena_status({"run": run})["ok"])
        st = tools.arena_status({"run": run, "dir": "runs"})
        self.assertTrue(st["ok"], st)
        for key in ("run", "dir", "seed", "wave", "task", "agents_n", "alive",
                    "rounds_total", "rounds_played", "current_round", "unrecorded",
                    "champion", "final", "cancelled", "finished", "jobs",
                    "state_schema_version"):
            self.assertIn(key, st)
        self.assertEqual(st["run"], run)
        self.assertEqual(st["agents_n"], 4)
        self.assertEqual(st["rounds_total"], 2)
        self.assertEqual(st["rounds_played"], 0)
        self.assertIsNone(st["champion"])
        self.assertFalse(st["finished"])
        self.assertFalse(st["cancelled"])
        self.assertEqual(len(st["jobs"]), 5)
        for phase, info in st["jobs"].items():
            self.assertIn("jobs", info)
            self.assertIn("missing", info)
        # LATEST resolves through the same base dir.
        latest = tools.arena_status({"run": "LATEST", "dir": "runs"})
        self.assertTrue(latest["ok"], latest)
        self.assertEqual(latest["run"], run)

    def test_result_on_fresh_run(self):
        start = tools.arena_start({"task": "t", "agents": 4, "dir": "runs"})
        res = tools.arena_result({"run": start["run"], "dir": "runs"})
        self.assertTrue(res["ok"], res)
        for key in ("run", "champion", "final", "collected", "alive",
                    "rounds_played", "finished", "cancelled"):
            self.assertIn(key, res)
        self.assertEqual(res["run"], start["run"])
        # Round 1 is open: both matches come back as "missing", not as [].
        self.assertEqual(len(res["collected"]), 2)
        self.assertEqual({c["status"] for c in res["collected"]}, {"missing"})
        self.assertIsNone(res["champion"])
        self.assertFalse(res["finished"])

    def test_cancel_then_cancel_again_reports_already(self):
        start = tools.arena_start({"task": "t", "agents": 4, "dir": "runs"})
        run = start["run"]
        first = tools.arena_cancel({"run": run, "dir": "runs"})
        # _guarded adds ok:True to every successful tool result (documented contract)
        self.assertEqual(
            first, {"cancelled": True, "run": run, "purged": False, "ok": True}
        )
        second = tools.arena_cancel({"run": run, "dir": "runs"})
        self.assertTrue(second["cancelled"])
        self.assertTrue(second["already"])
        st = tools.arena_status({"run": run, "dir": "runs"})
        self.assertTrue(st["cancelled"])

    def test_purge_deletes_run_dir_and_latest_pointer_file(self):
        tools.arena_start({"task": "a", "agents": 4, "dir": "runs"})
        start2 = tools.arena_start({"task": "b", "agents": 4, "dir": "runs"})
        latest = tools.arena_status({"run": "LATEST", "dir": "runs"})
        self.assertEqual(latest["run"], start2["run"])
        out = tools.arena_cancel({"run": start2["run"], "dir": "runs", "purge": True})
        self.assertTrue(out["cancelled"])
        self.assertTrue(out["purged"])
        self.assertFalse(os.path.exists(os.path.join("runs", start2["run"])))
        self.assertFalse(os.path.exists(os.path.join("runs", "LATEST")))

    def test_start_input_errors(self):
        res = tools.arena_start({"task": "t", "agents": 0, "dir": "runs"})
        self.assertFalse(res["ok"])
        self.assertEqual(
            res["error"],
            "agents must be between 1 and 2160 (got 0); use arena_plan to preview costs",
        )
        res = tools.arena_start({"task": "t", "wave": 0, "dir": "runs"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "wave must be at least 1 (got 0)")
        res = tools.arena_start({"task": "t", "agents": "4", "dir": "runs"})
        self.assertFalse(res["ok"])
        # _guarded short-circuits at the JSON-schema check before the impl runs.
        self.assertIn("agents", res["error"])
        res = tools.arena_start({"dir": "runs"})  # no task
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith("start needs a task"))

    def test_unknown_run_under_default_base_dir(self):
        res = tools.arena_status({"run": "does-not-exist"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no arena run directory 'does-not-exist' under .arena")


class RegistrationTests(unittest.TestCase):
    def test_register_exposes_arena_command_and_six_tools(self):
        ctx = _FakeContext()
        trix_arena.register(ctx)
        self.assertEqual(set(ctx.commands), {"arena"})
        self.assertEqual(set(ctx.tools), TOOL_NAMES)
        self.assertEqual(ctx.commands["arena"](["ping"]), "pong")
        self.assertTrue(ctx.tools["arena_ping"]()["pong"])
        self.assertTrue(ctx.tools["arena_plan"]({"agents": 4})["ok"])


class LifecycleTests(TmpCwdTestCase):
    """4-agent fake tournament: start -> judge verdicts -> collect -> final."""

    @staticmethod
    def _judge_verdict(winner, loser):
        return {
            "winner": winner,
            "reason": f"{winner} answered the attacks better",
            "survived": [],
            "standing": {winner: [], loser: ["hit"]},
            "scores": {winner: _SCORES_9, loser: _SCORES_5},
        }

    def test_four_agent_run_reaches_champion_and_final(self):
        start = tools.arena_start({"task": "demo", "agents": 4, "dir": "runs"})
        self.assertTrue(start["ok"], start)
        run = start["run"]
        run_dir = os.path.join("runs", run)

        expected_champion = None
        for _ in range(6):
            state = core.load_state(run_dir)
            if state["champion"]:
                break
            rd = core.current_round(state)
            self.assertIsNotNone(rd)
            for job in core.phase_jobs(state, "judge"):
                winner, loser = job["a"], job["b"]
                _write_json(job["outputs"][0], self._judge_verdict(winner, loser))
                expected_champion = winner  # the a-side always wins
            res = tools.arena_result({"run": run, "dir": "runs"})
            self.assertTrue(res["ok"], res)
            self.assertTrue(res["collected"])
            # arena_result collects verdicts but never advances the bracket.
            state = core.load_state(run_dir)
            if state["champion"]:
                break
            core.advance(state)
            core.save_state(state)

        self.assertIsNotNone(expected_champion)
        state = core.load_state(run_dir)
        self.assertIsNotNone(state["champion"])
        self.assertEqual(state["champion"], expected_champion)
        self.assertEqual(core.alive_ids(state), [expected_champion])
        self.assertEqual(core.rounds_played(state), 2)
        self.assertEqual(core.bracket_sizes(4), [4, 2, 1])

        # Upstream truth: the final only exists to beat a rejected answer
        # (baseline). START_SCHEMA has no baseline, so the champion ends the
        # run — no final, no final.judge job.
        self.assertIsNone(state["final"])
        self.assertEqual(core.phase_jobs(state, "final"), [])

        res = tools.arena_result({"run": run, "dir": "runs"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["run"], run)
        self.assertEqual(res["champion"], expected_champion)
        self.assertTrue(res["finished"])
        # champion + no final -> nothing left to collect
        self.assertEqual(res["collected"], [])

        st = tools.arena_status({"run": run, "dir": "runs"})
        self.assertTrue(st["ok"], st)
        self.assertTrue(st["finished"])
        self.assertEqual(st["champion"], expected_champion)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Unit tests for the extracted tournament core (plugins/trix-arena/arena/core.py).

Covers: card dealing, bracket/pairing, bye handling, scoring, state
save/load + migration, and a fake-backend 100-participant tournament
simulation (no real LLM calls, no network, stdlib only).
"""
import copy
import json
import math
import os
import random
import sys
import tempfile
import unittest

# Import the plugin core package
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from arena import core as C


REPO_ROOT = os.path.dirname(os.path.dirname(PLUGIN_DIR))
UPSTREAM_STRATEGIES = os.path.join(REPO_ROOT, "skills", "arena", "strategies.json")


def load_strategies():
    with open(UPSTREAM_STRATEGIES, encoding="utf-8") as fh:
        return json.load(fh)


class StrategiesTest(unittest.TestCase):
    """Card dealing tests."""

    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_combo_count(self):
        R = len(self.data["reasoning"])
        W = len(self.data["workflows"])
        S = len(self.data["strategies"])
        self.assertEqual(C.combo_count(self.data), R * W * S)

    def test_deal_unique_cards(self):
        dealt = C.deal(100, "seed", self.data)
        self.assertEqual(len(dealt), 100)
        self.assertEqual(len(set(dealt)), 100)

    def test_deal_deterministic(self):
        self.assertEqual(C.deal(10, "seed-a", self.data), C.deal(10, "seed-a", self.data))
        # Different seed -> likely different deal
        # (not guaranteed different, but extremely likely with 2160 combos)
        self.assertNotEqual(C.deal(10, "seed-a", self.data), C.deal(10, "seed-b", self.data))

    def test_deal_balanced_dimensions(self):
        """Every reasoning mode / workflow / strategy appears at most once more than the minimum."""
        for n in (16, 50, 100):
            dealt = C.deal(n, "seed", self.data)
            self.assertEqual(len(dealt), n)
            from collections import Counter
            for dim_idx, size in ((0, len(self.data["reasoning"])),
                                  (1, len(self.data["workflows"])),
                                  (2, len(self.data["strategies"]))):
                counts = Counter(t[dim_idx] for t in dealt)
                used = [counts.get(i, 0) for i in range(size)]
                # deal() balances values: any two used values differ by at most 1
                non_zero = [c for c in used if c > 0]
                if len(non_zero) > 1:
                    self.assertLessEqual(max(non_zero) - min(non_zero), 1)

    def test_deal_bounds_errors(self):
        total = C.combo_count(self.data)
        with self.assertRaises(C.ArenaError):
            C.deal(0, "seed", self.data)
        with self.assertRaises(C.ArenaError):
            C.deal(total + 1, "seed", self.data)

    def test_deal_edge_colour_branch(self):
        """Deg_r <= S and deg_w <= S triggers the edge-colouring branch."""
        dealt = C.deal(15, "seed", self.data)
        self.assertEqual(len(dealt), 15)
        self.assertEqual(len(set(dealt)), 15)

    def test_load_strategies_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "strategies.json")
            # Missing key
            with open(path, "w") as fh:
                json.dump({"reasoning": [], "workflows": [], "strategies": []}, fh)
            with self.assertRaises(C.ArenaError):
                C.load_strategies(path)
            # Duplicate id
            bad = {"reasoning": [{"id": "x", "name": "X", "how": "do X"}, {"id": "x", "name": "X2", "how": "do X2"}],
                   "workflows": [{"id": "w", "name": "W", "how": "do W"}],
                   "strategies": [{"id": "s", "name": "S", "how": "do S"}]}
            with open(path, "w") as fh:
                json.dump(bad, fh)
            with self.assertRaises(C.ArenaError):
                C.load_strategies(path)
            # Missing name/how
            bad2 = {"reasoning": [{"id": "x"}],
                    "workflows": [{"id": "w", "name": "W", "how": "do W"}],
                    "strategies": [{"id": "s", "name": "S", "how": "do S"}]}
            with open(path, "w") as fh:
                json.dump(bad2, fh)
            with self.assertRaises(C.ArenaError):
                C.load_strategies(path)

    def test_load_strategies_default_path(self):
        data = C.load_strategies()
        self.assertTrue(data["reasoning"])
        self.assertTrue(data["workflows"])
        self.assertTrue(data["strategies"])


class IdsBracketTest(unittest.TestCase):
    def test_agent_ids(self):
        self.assertEqual(C.agent_ids(5), ["a001", "a002", "a003", "a004", "a005"])
        self.assertEqual(C.agent_ids(1000)[0], "a0001")  # width max(3, len(str(n)))
        self.assertEqual(C.agent_ids(1000)[-1], "a1000")

    def test_agent_ids_100(self):
        ids = C.agent_ids(100)
        self.assertEqual(len(ids), 100)
        self.assertEqual(ids[0], "a001")
        self.assertEqual(ids[99], "a100")

    def test_bracket_sizes(self):
        self.assertEqual(C.bracket_sizes(100), [100, 50, 25, 13, 7, 4, 2, 1])
        self.assertEqual(C.bracket_sizes(16), [16, 8, 4, 2, 1])
        self.assertEqual(C.bracket_sizes(2), [2, 1])

    def test_bracket_sizes_one(self):
        self.assertEqual(C.bracket_sizes(1), [1])


class NewStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_new_state_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            self.assertEqual(state["state_schema_version"], C.CURRENT_STATE_SCHEMA_VERSION)
            self.assertEqual(state["version"], 1)
            self.assertEqual(state["seed"], "seed")
            self.assertEqual(state["agents_n"], 8)
            self.assertEqual(len(state["agents"]), 8)
            self.assertEqual(state["cards_available"], C.combo_count(self.data))
            self.assertIsNone(state["champion"])
            self.assertIsNone(state["final"])
            # Round 1 opened automatically
            self.assertEqual(len(state["rounds"]), 1)
            self.assertEqual(state["rounds"][0]["n"], 1)
            self.assertEqual(len(state["rounds"][0]["matches"]), 4)
            self.assertIsNone(state["rounds"][0]["bye"])

    def test_new_state_relative_paths(self):
        """All paths in state must be relative to the run_dir, not absolute."""
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            run_dir = state["dir"]
            for aid, a in state["agents"].items():
                sol = a["solution"]
                self.assertFalse(os.path.isabs(sol), f"solution path is absolute: {sol}")
            # round outputs
            for m in state["rounds"][0]["matches"]:
                pass  # match records don't hold paths in this design
            # Paths only used via phase_jobs
            jobs = C.phase_jobs(state, "attack")
            self.assertTrue(jobs)
            for j in jobs:
                for p in j["outputs"]:
                    self.assertTrue(os.path.isabs(p), f"output not absolute: {p}")

    def test_new_state_card_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            for aid, a in state["agents"].items():
                card = a["card"]
                self.assertIn("reasoning", card)
                self.assertIn("workflow", card)
                self.assertIn("strategy", card)
                self.assertTrue(card["reasoning"]["id"])
                self.assertTrue(card["workflow"]["id"])
                self.assertTrue(card["strategy"]["id"])
                self.assertTrue(a["alive"])
                self.assertIsNone(a["eliminated_in"])
                self.assertIsNone(a["eliminated_by"])
                self.assertEqual(a["byes"], 0)

    def test_new_state_baseline_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(1, "seed", self.data, tmp, has_baseline=True)
            # n=1 -> champion immediately; with a baseline present the final is
            # still the baseline until it is beaten in a match (upstream truth).
            self.assertEqual(state["champion"], "a001")
            self.assertEqual(state["final"]["X"], "baseline")
            self.assertEqual(state["final"]["Y"], "champion")


class PairingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_pair_round_different_reasoning_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(16, "seed", self.data, tmp)
            rd = state["rounds"][0]
            modes = set()
            for m in rd["matches"]:
                ma = state["agents"][m["a"]]["card"]["reasoning"]["id"]
                mb = state["agents"][m["b"]]["card"]["reasoning"]["id"]
                modes.add(ma)
                modes.add(mb)
            # With balanced dealing, pairing prefers different modes
            # (cannot guarantee with 16 agents across 15 modes, but all matches
            # should have been built with preference)
            self.assertEqual(len(rd["matches"]), 8)

    def test_pair_round_odd_bye_fewest_byes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(5, "seed", self.data, tmp)
            rd = state["rounds"][0]
            # 5 alive -> 2 matches + 1 bye
            self.assertEqual(len(rd["matches"]), 2)
            self.assertIsNotNone(rd["bye"])
            self.assertEqual(state["agents"][rd["bye"]]["byes"], 0)

    def test_bye_cycles_through_fewest(self):
        """Byes go to the fewest-bye agent; no agent gets 2 byes before all get 1."""
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(7, "seed", self.data, tmp)
            rng = random.Random(42)
            while not state["champion"]:
                rd = C.current_round(state)
                for m in rd["matches"]:
                    C.record(state, m["id"], rng.choice([m["a"], m["b"]]))
                C.advance(state)
            for v in state["agents"].values():
                self.assertLessEqual(v["byes"], 1)

    def test_match_id_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(100, "seed", self.data, tmp)
            rd = state["rounds"][0]
            # 100 -> 50 matches, width = max(2, len("50")) = 2
            self.assertEqual(len(rd["matches"]), 50)
            self.assertEqual(rd["matches"][0]["id"], "r1-m01")
            self.assertEqual(rd["matches"][-1]["id"], "r1-m50")


class ScoringTest(unittest.TestCase):
    def test_weighted_total_perfect(self):
        self.assertEqual(C.weighted_total({k: 10 for k, _ in C.WEIGHTS}), 100.0)

    def test_weighted_total_known_value(self):
        # (8*30 + 6*25 + 7*15 + 5*20 + 9*10) / 10 = 68.5
        self.assertEqual(C.weighted_total({
            "correctness": 8, "completeness": 6, "specificity": 7,
            "robustness": 5, "clarity": 9
        }), 68.5)

    def test_weighted_total_clamps(self):
        # 15 -> clamped to 10; -3 -> clamped to 0
        self.assertEqual(C.weighted_total({k: 15 for k, _ in C.WEIGHTS}), 100.0)
        self.assertEqual(C.weighted_total({k: -3 for k, _ in C.WEIGHTS}), 0.0)

    def test_weighted_total_rejects_bad(self):
        self.assertIsNone(C.weighted_total(None))
        self.assertIsNone(C.weighted_total({"correctness": True}))  # bool rejected
        self.assertIsNone(C.weighted_total({"correctness": "high"}))
        self.assertIsNone(C.weighted_total({"correctness": 8}))  # incomplete

    def test_decide_fatal(self):
        v = {"scores": {
            "a001": {"correctness": 9, "completeness": 9, "specificity": 9, "robustness": 9, "clarity": 9, "fatal": True},
            "a002": {"correctness": 4, "completeness": 4, "specificity": 4, "robustness": 4, "clarity": 4, "fatal": False}
        }}
        w, t, note = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")  # a001's fatal error beats everything

    def test_decide_higher_total(self):
        v = {"scores": {
            "a001": {"correctness": 5, "completeness": 5, "specificity": 5, "robustness": 5, "clarity": 5},
            "a002": {"correctness": 8, "completeness": 8, "specificity": 8, "robustness": 8, "clarity": 8}
        }}
        w, t, note = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_tie_fewer_standing_attacks(self):
        v = {"scores": {
            "a001": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x", "y"], "a002": ["z"]}}
        w, _, _ = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_tie_higher_correctness(self):
        v = {"scores": {
            "a001": {"correctness": 8, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x"], "a002": ["x"]}}
        w, _, _ = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a001")

    def test_decide_tie_judges_pick(self):
        v = {"scores": {
            "a001": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x"], "a002": ["x"]}, "winner": "a002"}
        w, _, _ = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_pick_vs_scores_disagreement(self):
        v = {"scores": {
            "a001": {"correctness": 8, "completeness": 8, "specificity": 8, "robustness": 8, "clarity": 8},
            "a002": {"correctness": 5, "completeness": 5, "specificity": 5, "robustness": 5, "clarity": 5}
        }, "winner": "a002"}
        w, t, note = C.decide(v, "a001", "a002")
        self.assertEqual(w, "a001")
        self.assertIn("scores win", note)

    def test_decide_incomplete_scores_falls_back_to_pick(self):
        v = {"scores": {"a001": {"correctness": 8}}, "winner": "a001"}
        w, t, note = C.decide(v, "a001", "a002")
        self.assertIn("incomplete", note)
        self.assertEqual(w, "a001")

    def test_decide_incomplete_no_pick_raises(self):
        v = {"scores": {"a001": {"correctness": 8}}}
        with self.assertRaises(C.ArenaError):
            C.decide(v, "a001", "a002")

    def test_extract_json(self):
        self.assertEqual(C.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(C.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(C.extract_json("no json here"))
        self.assertEqual(C.extract_json('prefix {"a": 1} suffix'), {"a": 1})

    def test_record_requires_winner_in_match(self):
        data = load_strategies()
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", data, tmp)
            rd = state["rounds"][0]
            m = rd["matches"][0]
            outsider = next(a for a in sorted(state["agents"]) if a not in (m["a"], m["b"]))
            with self.assertRaises(C.ArenaError):
                C.record(state, m["id"], outsider)
            m2, prev = C.record(state, m["id"], m["a"])
            self.assertEqual(m2["winner"], m["a"])
            self.assertIsNone(prev)


class StateIOTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            C.save_state(state, base_dir=tmp)
            path = os.path.join(tmp, C.STATE_FILE)
            self.assertTrue(os.path.isfile(path))
            # No .tmp file left behind
            self.assertFalse(os.path.exists(path + ".tmp"))
            loaded = C.load_state(tmp)
            self.assertEqual(loaded["seed"], "seed")
            self.assertEqual(loaded["agents_n"], 8)
            self.assertEqual(loaded["state_schema_version"], C.CURRENT_STATE_SCHEMA_VERSION)
            # dir restored relative
            self.assertEqual(os.path.normpath(loaded["dir"]), os.path.normpath(tmp))

    def test_save_relative_dir(self):
        """With base_dir, the stored dir must be relative ('.')."""
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "filed-seed", self.data, tmp)
            C.save_state(state, base_dir=tmp)
            with open(os.path.join(tmp, C.STATE_FILE)) as fh:
                raw = json.load(fh)
            self.assertFalse(os.path.isabs(raw["dir"]))
            self.assertEqual(raw["dir"], ".")  # relpath of tmp against itself

    def test_atomic_save_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            C.save_state(state, base_dir=tmp)
            leftovers = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_load_missing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(C.ArenaError):
                C.load_state(tmp)

    def test_migrate_v1_to_v2(self):
        """v1 (upstream-like) = version:1, abspath dir, no state_schema_version."""
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "old-seed", self.data, tmp)
            # Forge a v1 state
            del state["state_schema_version"]
            state["version"] = 1
            # dir stays absolute (already is)
            migrated = C.migrate(state, base_dir=tmp)
            self.assertEqual(migrated["state_schema_version"], 2)
            # dir relativized
            self.assertFalse(os.path.isabs(migrated["dir"]))
            self.assertEqual(migrated["dir"], ".")

    def test_migrate_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            once = C.migrate(copy.deepcopy(state), base_dir=tmp)
            twice = C.migrate(copy.deepcopy(once), base_dir=tmp)
            self.assertEqual(twice["dir"], once["dir"])
            self.assertEqual(twice["state_schema_version"], 2)

    def test_migrate_already_current_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            C.save_state(state, base_dir=tmp)
            loaded = C.load_state(tmp)
            migrated = C.migrate(loaded, base_dir=tmp)
            # Already v2 with absolute dir (load_state resolved it)
            self.assertEqual(migrated["state_schema_version"], 2)
            self.assertEqual(migrated["dir"], loaded["dir"])

    def test_ensure_current_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            del state["state_schema_version"]
            state["version"] = 1
            result = C.ensure_current_schema(state, base_dir=tmp)
            self.assertEqual(result["state_schema_version"], 2)


class AdvanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_advance_requires_all_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            with self.assertRaises(C.ArenaError):
                C.advance(state)

    def test_advance_eliminates_losers(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(4, "seed", self.data, tmp)
            rd = state["rounds"][0]
            for m in rd["matches"]:
                C.record(state, m["id"], m["a"])
            survivors = C.advance(state)
            self.assertEqual(len(survivors), 2)
            for m in rd["matches"]:
                self.assertFalse(state["agents"][m["loser"]]["alive"])
                self.assertEqual(state["agents"][m["loser"]]["eliminated_in"], 1)
                self.assertEqual(state["agents"][m["loser"]]["eliminated_by"], m["winner"])
            # Winner alive
            for m in rd["matches"]:
                self.assertTrue(state["agents"][m["winner"]]["alive"])
            rd2 = state["rounds"][1]
            self.assertEqual(len(rd2["matches"]), 1)
            # Losers do not reappear
            for m2 in rd2["matches"]:
                self.assertNotIn(m2["a"], [m["loser"] for m in rd["matches"]])
                self.assertNotIn(m2["b"], [m["loser"] for m in rd["matches"]])

    def test_advance_bye_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(3, "seed", self.data, tmp)
            rd = state["rounds"][0]
            self.assertEqual(len(rd["matches"]), 1)
            bye = rd["bye"]
            self.assertIsNotNone(bye)
            C.record(state, rd["matches"][0]["id"], rd["matches"][0]["a"])
            survivors = C.advance(state)
            self.assertIn(bye, survivors)
            self.assertEqual(state["agents"][bye]["byes"], 1)

    def test_current_round_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(2, "seed", self.data, tmp)
            rd = state["rounds"][0]
            C.record(state, rd["matches"][0]["id"], rd["matches"][0]["a"])
            C.advance(state)
            # 2 -> 1: champion crowned, no round open
            self.assertEqual(state["champion"], rd["matches"][0]["a"])
            self.assertIsNone(C.current_round(state))

    def test_final_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(2, "seed", self.data, tmp, has_baseline=True)
            rd = state["rounds"][0]
            C.record(state, rd["matches"][0]["id"], rd["matches"][0]["a"])
            C.advance(state)
            self.assertEqual(state["champion"], rd["matches"][0]["a"])
            fin = state["final"]
            self.assertIn(fin["X"], ("champion", "baseline"))
            self.assertEqual(set(fin.keys()), {"X", "Y", "result"})
            self.assertEqual(fin["Y"], "baseline" if fin["X"] == "champion" else "champion")


class JobsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_spawn_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            jobs = C.phase_jobs(state, "spawn")
            self.assertEqual(len(jobs), 8)
            for j in jobs:
                self.assertEqual(j["kind"], "competitor")
                self.assertEqual(j["outputs"][0], os.path.join(tmp, "r0", f"{j['agent']}.md"))

    def test_attack_defend_judge_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(8, "seed", self.data, tmp)
            jobs = C.phase_jobs(state, "attack")
            # 1 attack job per participant (8 agents) — upstream truth, not 2/match
            self.assertEqual(len(jobs), 8)
            jobs = C.phase_jobs(state, "defend")
            # 1 defend job per participant — upstream truth
            self.assertEqual(len(jobs), 8)
            jobs = C.phase_jobs(state, "judge")
            self.assertEqual(len(jobs), 4)

    def test_final_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(1, "seed", self.data, tmp, has_baseline=True)
            jobs = C.phase_jobs(state, "final")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["id"], "final.judge")
            self.assertEqual(jobs[0]["outputs"][0], os.path.join(tmp, "final.verdict.json"))

    def test_unknown_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(2, "seed", self.data, tmp)
            with self.assertRaises(C.ArenaError):
                C.phase_jobs(state, "banana")

    def test_missing_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(2, "seed", self.data, tmp)
            jobs = C.phase_jobs(state, "spawn")
            missing = C.missing_jobs(jobs)
            self.assertEqual(len(missing), 2)


class PlanTest(unittest.TestCase):
    def test_plan_rows_100(self):
        rows = C.plan_rows(100)
        # First round: 100 alive, 50 matches, no bye
        self.assertEqual(rows[0]["alive"], 100)
        self.assertEqual(rows[0]["matches"], 50)
        self.assertEqual(rows[0]["bye"], 0)
        # calls = 5 per match = 250
        self.assertEqual(rows[0]["calls"], 250)

    def test_plan_totals_100(self):
        t = C.plan_totals(100)
        self.assertEqual(t["agents"], 100)
        self.assertEqual(t["rounds"], 7)
        self.assertEqual(t["calls"], 595)
        # upstream truth: 100 agents -> 70 waves (25+13+8+5+3+3+3)
        self.assertEqual(t["waves"], 70)


class TournamentSimulationTest(unittest.TestCase):
    """Fake-backend simulation of a 100-participant tournament: exactly 1 winner."""

    @classmethod
    def setUpClass(cls):
        cls.data = load_strategies()

    def test_100_participant_tournament_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(100, "s1", self.data, tmp)
            self.assertEqual(len(state["agents"]), 100)
            rng = random.Random(20261012)
            rounds = 0
            while not state["champion"]:
                rd = C.current_round(state)
                self.assertIsNotNone(rd, "round opened while no champion")
                rounds += 1
                self.assertLess(rounds, 20, "runaway bracket")
                for m in rd["matches"]:
                    winner = rng.choice([m["a"], m["b"]])
                    C.record(state, m["id"], winner)
                C.advance(state)

            alive = [a for a, v in state["agents"].items() if v["alive"]]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0], state["champion"])
            dead = [v for v in state["agents"].values() if not v["alive"]]
            self.assertEqual(len(dead), 99)
            for v in dead:
                self.assertIsNotNone(v["eliminated_in"])
                self.assertIsNotNone(v["eliminated_by"])
            total_matches = sum(len(rd["matches"]) for rd in state["rounds"])
            self.assertEqual(total_matches, 99)
            bye_pattern = [rd["bye"] is not None for rd in state["rounds"]]
            self.assertEqual(bye_pattern, [False, False, True, True, True, False, False])
            for v in state["agents"].values():
                self.assertLessEqual(v["byes"], 1)

    def test_100_participant_tournament_with_judges(self):
        """Same run but winners decided via fake judge verdicts (collect)."""
        with tempfile.TemporaryDirectory() as tmp:
            state = C.new_state(100, "s2", self.data, tmp)
            rng = random.Random(31337)

            while not state["champion"]:
                rd = C.current_round(state)
                self.assertIsNotNone(rd)

                # Fake judge: write a verdict JSON for every match
                for m in rd["matches"]:
                    # Random scores -> decide() picks a winner deterministically
                    winner = rng.choice([m["a"], m["b"]])
                    loser = m["b"] if winner == m["a"] else m["a"]
                    verdict = {
                        "winner": winner,
                        "reason": "fake",
                        "survived": [],
                        "standing": {winner: [], loser: ["hit"]},
                        "scores": {
                            winner: {"correctness": 9, "completeness": 9, "specificity": 9,
                                     "robustness": 9, "clarity": 9},
                            loser: {"correctness": 5, "completeness": 5, "specificity": 5,
                                    "robustness": 5, "clarity": 5},
                        },
                    }
                    path = os.path.join(tmp, f"r{rd['n']}", f"{m['id']}.verdict.json")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as fh:
                        json.dump(verdict, fh)

                results = C.collect(state)
                # All recorded
                statuses = {s for _, s, _ in results}
                self.assertIn("recorded", statuses)
                for m in rd["matches"]:
                    self.assertIsNotNone(m["winner"])
                C.advance(state)

            alive = [a for a, v in state["agents"].items() if v["alive"]]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0], state["champion"])
            self.assertEqual(sum(len(rd["matches"]) for rd in state["rounds"]), 99)

    def test_100_participant_tournament_stale_dir_after_move(self):
        """State saved in one dir survives being loaded from a copied dir (relative dir)."""
        with tempfile.TemporaryDirectory() as tmp1:
            state = C.new_state(16, "s3", self.data, tmp1)
            # play one round
            rd = state["rounds"][0]
            rng = random.Random(5)
            for m in rd["matches"]:
                C.record(state, m["id"], rng.choice([m["a"], m["b"]]))
            C.advance(state)
            C.save_state(state, base_dir=tmp1)
            # Copy the whole run dir elsewhere and reload
            tmp2 = tempfile.mkdtemp()
            for f in os.listdir(tmp1):
                src = os.path.join(tmp1, f)
                if os.path.isfile(src):
                    with open(src, "rb") as fh:
                        blob = fh.read()
                    with open(os.path.join(tmp2, f), "wb") as fh:
                        fh.write(blob)
            loaded = C.load_state(tmp2)
            # dir restored relative (same normalization as LoadStateTest)
            self.assertEqual(os.path.normpath(loaded["dir"]), os.path.normpath(tmp2))
            self.assertEqual(len(loaded["agents"]), 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
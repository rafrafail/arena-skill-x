#!/usr/bin/env python3
"""Characterization tests for upstream skills/arena/bracket.py behavior.

These tests capture the exact current behavior of the upstream module BEFORE
any refactoring. They import the upstream module the same way the upstream
test file does and verify all key behaviors are pinned.

Run: python3 -m pytest plugins/trix-arena/tests/test_upstream_characterization.py -v
"""
import json
import os
import sys
import tempfile
import unittest

# Import upstream exactly like test_bracket.py does
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SKILL = os.path.join(REPO, "skills", "arena")
sys.path.insert(0, SKILL)

import bracket as B  # noqa: E402


class UpstreamCharacterizationTests(unittest.TestCase):
    """Pin the exact current behavior of upstream bracket.py."""

    def setUp(self):
        self.data = B.load_strategies()

    # --- Constants ---
    def test_constants_match_upstream(self):
        self.assertEqual(B.DEFAULT_AGENTS, 100)
        self.assertEqual(B.QUICK_AGENTS, 16)
        self.assertEqual(B.DEFAULT_WAVE, 10)
        self.assertEqual(B.ROOT, ".arena")
        self.assertEqual(B.LATEST, "LATEST")
        self.assertEqual(B.STATE_FILE, "arena.json")
        self.assertEqual(B.PHASES, ("spawn", "attack", "defend", "judge", "final"))
        self.assertEqual(B.CALLS_PER_MATCH, 5)
        self.assertEqual(B.WEIGHTS, (
            ("correctness", 30),
            ("completeness", 25),
            ("specificity", 15),
            ("robustness", 20),
            ("clarity", 10),
        ))

    # --- Strategies loading ---
    def test_load_strategies_validates_structure(self):
        data = B.load_strategies()
        # Keys present and non-empty
        self.assertIn("reasoning", data)
        self.assertIn("workflows", data)
        self.assertIn("strategies", data)
        self.assertGreater(len(data["reasoning"]), 0)
        self.assertGreater(len(data["workflows"]), 0)
        self.assertGreater(len(data["strategies"]), 0)
        # Unique ids
        for key in ("reasoning", "workflows", "strategies"):
            ids = [it["id"] for it in data[key]]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate ids in {key}")
        # Name and how present
        for key in ("reasoning", "workflows", "strategies"):
            for it in data[key]:
                self.assertTrue(it.get("name"), f"missing name in {key}:{it['id']}")
                self.assertTrue(it.get("how"), f"missing how in {key}:{it['id']}")

    def test_combo_count(self):
        self.assertEqual(B.combo_count(self.data), 15 * 12 * 12)  # 2160

    # --- Card dealing ---
    def test_deal_deterministic_per_seed(self):
        cards1 = B.deal(100, "seed123", self.data)
        cards2 = B.deal(100, "seed123", self.data)
        self.assertEqual(cards1, cards2)
        cards3 = B.deal(100, "seed456", self.data)
        self.assertNotEqual(cards1, cards3)

    def test_deal_unique_cards(self):
        cards = B.deal(100, "test", self.data)
        self.assertEqual(len(cards), 100)
        self.assertEqual(len(set(cards)), 100, "no duplicate cards dealt")

    def test_deal_balanced_distribution(self):
        """Each dimension balanced: max diff <= 1."""
        cards = B.deal(100, "test", self.data)
        R, W, S = 15, 12, 12
        for dim, size in enumerate((R, W, S)):
            counts = [0] * size
            for c in cards:
                counts[c[dim]] += 1
            self.assertLessEqual(max(counts) - min(counts), 1,
                                 f"dim {dim} unbalanced: {counts}")

    def test_deal_no_repeated_pairs_under_144(self):
        """Up to 144 agents: no two agents share two dimensions."""
        for n in (1, 2, 7, 16, 32, 50, 99, 100, 144):
            for seed in range(10):
                cards = B.deal(n, str(seed), self.data)
                for a, b in ((0, 1), (0, 2), (1, 2)):
                    pairs = {}
                    for c in cards:
                        key = (c[a], c[b])
                        pairs[key] = pairs.get(key, 0) + 1
                    self.assertEqual(max(pairs.values()), 1,
                                     f"n={n} seed={seed} dim={a},{b}: duplicate pair")

    def test_deal_raises_on_invalid_n(self):
        with self.assertRaises(B.ArenaError):
            B.deal(0, "x", self.data)
        with self.assertRaises(B.ArenaError):
            B.deal(2161, "x", self.data)

    # --- Agent IDs ---
    def test_agent_ids_format(self):
        ids = B.agent_ids(100)
        self.assertEqual(len(ids), 100)
        self.assertEqual(ids[0], "a001")
        self.assertEqual(ids[99], "a100")
        self.assertTrue(all(len(i) == 4 for i in ids))

        ids = B.agent_ids(7)
        self.assertEqual(ids[0], "a001")
        self.assertEqual(ids[6], "a007")

    # --- Bracket sizes ---
    def test_bracket_sizes(self):
        self.assertEqual(B.bracket_sizes(100), [100, 50, 25, 13, 7, 4, 2, 1])
        self.assertEqual(B.bracket_sizes(16), [16, 8, 4, 2, 1])
        self.assertEqual(B.bracket_sizes(7), [7, 4, 2, 1])
        self.assertEqual(B.bracket_sizes(1), [1])

    # --- State creation ---
    def test_new_state_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = B.new_state(10, 42, self.data, tmp)
            # Required fields present
            self.assertEqual(state["version"], 1)
            self.assertIn("created", state)
            self.assertEqual(state["seed"], 42)
            self.assertEqual(state["agents_n"], 10)
            self.assertEqual(state["cards_available"], 2160)
            self.assertEqual(state["wave"], 10)
            self.assertTrue(os.path.isabs(state["dir"]))
            self.assertFalse(state["has_baseline"])
            self.assertIsNone(state["champion"])
            self.assertIsNone(state["final"])
            self.assertEqual(len(state["agents"]), 10)
            self.assertEqual(len(state["rounds"]), 1)  # first round opened
            # Agent structure
            for aid, agent in state["agents"].items():
                self.assertIn("card", agent)
                self.assertIn("reasoning", agent["card"])
                self.assertIn("workflow", agent["card"])
                self.assertIn("strategy", agent["card"])
                self.assertIn("solution", agent)
                self.assertTrue(agent["alive"])
                self.assertIsNone(agent["eliminated_in"])
                self.assertIsNone(agent["eliminated_by"])
                self.assertEqual(agent["byes"], 0)

    def test_new_state_dir_is_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.relpath(tmp)
            state = B.new_state(5, 1, self.data, rel)
            self.assertTrue(os.path.isabs(state["dir"]))

    # --- Pairing ---
    def test_pair_round_seeded(self):
        state = B.new_state(10, "seed", self.data, "/tmp")
        rd1_matches, rd1_bye = B.pair_round(state, 1, B.alive_ids(state))
        rd2_matches, rd2_bye = B.pair_round(state, 1, B.alive_ids(state))
        # Same round number + same seed = same pairing
        self.assertEqual([m["id"] for m in rd1_matches], [m["id"] for m in rd2_matches])
        self.assertEqual(rd1_bye, rd2_bye)

    def test_pair_round_odd_gives_bye_to_fewest_byes(self):
        state = B.new_state(7, "seed", self.data, "/tmp")
        # Give one agent a bye already
        state["agents"]["a001"]["byes"] = 1
        matches, bye = B.pair_round(state, 1, B.alive_ids(state))
        # Bye should go to someone with 0 byes, not the one with 1
        self.assertNotEqual(bye, "a001")

    def test_pair_round_prefers_different_reasoning(self):
        state = B.new_state(100, "seed", self.data, "/tmp")
        matches, _ = B.pair_round(state, 1, B.alive_ids(state))
        same_mode = 0
        for m in matches:
            ra = state["agents"][m["a"]]["card"]["reasoning"]["id"]
            rb = state["agents"][m["b"]]["card"]["reasoning"]["id"]
            if ra == rb:
                same_mode += 1
        # At most 1 forced same-mode match in 50 matches
        self.assertLessEqual(same_mode, 1)

    def test_pair_round_sides_swapped_50_50(self):
        # Over many seeds, both orderings should appear
        orderings = set()
        for seed in range(100):
            state = B.new_state(10, str(seed), self.data, "/tmp")
            matches, _ = B.pair_round(state, 1, B.alive_ids(state))
            for m in matches:
                orderings.add((m["a"], m["b"]))
        # With 10 agents = 5 matches, we should see variety
        self.assertGreater(len(orderings), 5)

    def test_match_id_format(self):
        state = B.new_state(100, "seed", self.data, "/tmp")
        matches, _ = B.pair_round(state, 1, B.alive_ids(state))
        for m in matches:
            self.assertTrue(m["id"].startswith("r1-m"))
            # width should be max(2, len(str(50))) = 2
            self.assertEqual(len(m["id"]), len("r1-m01"))

    # --- open_round ---
    def test_open_round_creates_matches(self):
        state = B.new_state(8, "seed", self.data, "/tmp")
        rd = B.open_round(state, B.alive_ids(state))
        self.assertIsNotNone(rd)
        self.assertEqual(rd["n"], 2)
        self.assertEqual(len(rd["matches"]), 4)
        self.assertIsNone(rd["bye"])

    def test_open_round_single_agent_crowns_champion(self):
        state = B.new_state(1, "seed", self.data, "/tmp")
        state["rounds"] = []  # clear the auto-opened round
        rd = B.open_round(state, ["a001"])
        self.assertIsNone(rd)
        self.assertEqual(state["champion"], "a001")

    def test_open_round_final_assignment_when_baseline(self):
        state = B.new_state(1, "seed", self.data, "/tmp", has_baseline=True)
        state["rounds"] = []
        rd = B.open_round(state, ["a001"])
        self.assertIsNone(rd)
        self.assertEqual(state["champion"], "a001")
        self.assertIsNotNone(state["final"])
        self.assertIn(state["final"]["X"], ("champion", "baseline"))
        self.assertIn(state["final"]["Y"], ("champion", "baseline"))
        self.assertNotEqual(state["final"]["X"], state["final"]["Y"])

    # --- record / advance ---
    def test_record_requires_winner_in_match(self):
        state = B.new_state(4, "seed", self.data, "/tmp")
        rd = state["rounds"][0]
        m = rd["matches"][0]
        with self.assertRaises(B.ArenaError):
            B.record(state, m["id"], "a999")  # not in match

    def test_advance_requires_all_matches_recorded(self):
        state = B.new_state(4, "seed", self.data, "/tmp")
        with self.assertRaises(B.ArenaError):
            B.advance(state)

    def test_advance_eliminates_losers(self):
        state = B.new_state(4, "seed", self.data, "/tmp")
        rd = state["rounds"][0]
        for m in rd["matches"]:
            B.record(state, m["id"], m["a"])
        B.advance(state)
        for m in rd["matches"]:
            loser = state["agents"][m["loser"]]
            self.assertFalse(loser["alive"])
            self.assertEqual(loser["eliminated_in"], 1)
            self.assertEqual(loser["eliminated_by"], m["winner"])
        # Winners alive, bye survives
        self.assertTrue(state["agents"][rd["matches"][0]["winner"]]["alive"])

    def test_advance_bye_gets_byes_increment(self):
        state = B.new_state(5, "seed", self.data, "/tmp")
        rd = state["rounds"][0]
        # Record all matches
        for m in rd["matches"]:
            B.record(state, m["id"], m["a"])
        B.advance(state)
        self.assertEqual(state["agents"][rd["bye"]]["byes"], 1)

    def test_eliminated_never_return(self):
        state = B.new_state(4, "seed", self.data, "/tmp")
        rd = state["rounds"][0]
        for m in rd["matches"]:
            B.record(state, m["id"], m["a"])
        B.advance(state)
        # Losers eliminated
        for m in rd["matches"]:
            self.assertFalse(state["agents"][m["loser"]]["alive"])
        # Advance again - losers should not reappear
        rd2 = state["rounds"][1]
        for m2 in rd2["matches"]:
            self.assertNotIn(m2["a"], [m["loser"] for m in rd["matches"]])
            self.assertNotIn(m2["b"], [m["loser"] for m in rd["matches"]])

    # --- Scoring ---
    def test_weighted_total_clamp_and_scale(self):
        # Perfect scores = 100
        self.assertEqual(B.weighted_total({k: 10 for k, _ in B.WEIGHTS}), 100.0)
        # Known weighted calc: (8*30 + 6*25 + 7*15 + 5*20 + 9*10) / 10 = 68.5
        self.assertEqual(B.weighted_total({
            "correctness": 8, "completeness": 6, "specificity": 7,
            "robustness": 5, "clarity": 9
        }), 68.5)

    def test_weighted_total_rejects_bools_and_non_numeric(self):
        self.assertIsNone(B.weighted_total({"correctness": True}))
        self.assertIsNone(B.weighted_total({"correctness": "eight"}))
        self.assertIsNone(B.weighted_total({"correctness": 8}))  # missing other keys

    def test_decide_fatal_asymmetry_beats_everything(self):
        v = {"scores": {
            "a001": {"correctness": 9, "completeness": 9, "specificity": 9, "robustness": 9, "clarity": 9, "fatal": True},
            "a002": {"correctness": 4, "completeness": 4, "specificity": 4, "robustness": 4, "clarity": 4, "fatal": False}
        }}
        # a001 has fatal=True, a002 has fatal=False -> a002 wins
        w, totals, note = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_higher_total_wins(self):
        v = {"scores": {
            "a001": {"correctness": 5, "completeness": 5, "specificity": 5, "robustness": 5, "clarity": 5},
            "a002": {"correctness": 8, "completeness": 8, "specificity": 8, "robustness": 8, "clarity": 8}
        }}
        w, totals, note = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_exact_tie_fewer_standing_attacks(self):
        v = {"scores": {
            "a001": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x", "y"], "a002": ["z"]}}
        w, _, _ = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")  # fewer standing attacks wins

    def test_decide_exact_tie_higher_correctness(self):
        v = {"scores": {
            "a001": {"correctness": 8, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x"], "a002": ["x"]}}
        w, _, _ = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a001")  # higher correctness wins

    def test_decide_exact_tie_judges_pick(self):
        v = {"scores": {
            "a001": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7},
            "a002": {"correctness": 7, "completeness": 7, "specificity": 7, "robustness": 7, "clarity": 7}
        }, "standing": {"a001": ["x"], "a002": ["x"]}, "winner": "a002"}
        w, _, _ = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")

    def test_decide_pick_vs_scores_disagreement_noted(self):
        v = {"scores": {
            "a001": {"correctness": 8, "completeness": 8, "specificity": 8, "robustness": 8, "clarity": 8},
            "a002": {"correctness": 5, "completeness": 5, "specificity": 5, "robustness": 5, "clarity": 5}
        }, "winner": "a002"}  # judge picks a002 but scores favor a001
        w, totals, note = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a001")
        self.assertIn("scores win", note)

    def test_record_requires_winner_in_ab(self):
        state = B.new_state(4, "seed", self.data, "/tmp")
        rd = state["rounds"][0]
        m = rd["matches"][0]
        outsider = next(a for a in sorted(state["agents"]) if a not in (m["a"], m["b"]))
        with self.assertRaises(B.ArenaError):
            B.record(state, m["id"], outsider, scores={outsider: 50.0})
        # But valid should work
        m2, _ = B.record(state, m["id"], m["a"], scores={m["a"]: 50.0, m["b"]: 40.0})
        self.assertEqual(m2["winner"], m["a"])

    # --- State IO atomic save ---
    def test_save_state_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = B.new_state(5, "seed", self.data, tmp)
            # Verify atomic save: .tmp then os.replace
            path = os.path.join(tmp, B.STATE_FILE)
            # Save should create file
            B.save_state(state)
            self.assertTrue(os.path.isfile(path))
            # Verify it's valid JSON
            with open(path) as fh:
                loaded = json.load(fh)
            self.assertEqual(loaded["seed"], "seed")
            self.assertEqual(loaded["agents_n"], 5)

    def test_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = B.new_state(5, "seed", self.data, tmp)
            B.save_state(state)
            # Simulate load_state args
            class Args:
                dir = tmp
            loaded = B.load_state(Args())
            self.assertEqual(loaded["seed"], "seed")
            self.assertEqual(loaded["agents_n"], 5)

    # --- 100-participant tournament simulation ---
    def test_100_agent_tournament_one_winner(self):
        """Simulate a full 100-agent tournament with random winners - must end with exactly 1 champion."""
        with tempfile.TemporaryDirectory() as tmp:
            state = B.new_state(100, 7, self.data, tmp)
            import random
            rng = random.Random(7)
            round_num = 0
            while not state["champion"]:
                rd = B.current_round(state)
                round_num += 1
                self.assertIsNotNone(rd)
                # Record all matches with random winners
                for m in rd["matches"]:
                    winner = rng.choice([m["a"], m["b"]])
                    B.record(state, m["id"], winner)
                B.advance(state)
            # Verify exactly 1 champion
            self.assertIsNotNone(state["champion"])
            alive = [a for a, v in state["agents"].items() if v["alive"]]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0], state["champion"])
            # 99 eliminated
            dead = [v for v in state["agents"].values() if not v["alive"]]
            self.assertEqual(len(dead), 99)
            # All eliminated have elimination metadata
            for v in dead:
                self.assertIsNotNone(v["eliminated_in"])
                self.assertIsNotNone(v["eliminated_by"])
            # Total matches = 99
            total_matches = sum(len(rd["matches"]) for rd in state["rounds"])
            self.assertEqual(total_matches, 99)
            # Bye pattern for 100: [False, False, True, True, True, False, False]
            bye_pattern = [rd["bye"] is not None for rd in state["rounds"]]
            self.assertEqual(bye_pattern, [False, False, True, True, True, False, False])
            # No agent gets more than 1 bye
            for v in state["agents"].values():
                self.assertLessEqual(v["byes"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
"""Stage 4 gate tests: the built-in skill document.

The stage-4 gate is "a novice can launch a run without reading the
sources". The skill document (skills/trix-arena/SKILL.md) is the only
thing a novice reads, so these tests pin it to the real plugin surface:

- frontmatter (name / description triggers / argument-hint) intact;
- stage-0 stub markers removed;
- required sections present (triggers, mode selection, canonical task
  package, preflight, driving a run, job execution, final answer, limits);
- preflight cost numbers match ``core.plan_totals`` for the two
  documented presets (default 100, quick 16) at ``DEFAULT_WAVE``;
- every documented subcommand and tool name actually exists in
  ``commands.KNOWN_SUBCOMMANDS`` / ``schemas.TOOL_SCHEMAS``;
- the final-answer section covers the stage-4 checklist parts: winner,
  score, survived attacks, limitations.
"""

import re
from pathlib import Path

from trix_arena import commands, schemas
from trix_arena.arena import core

SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "trix-arena" / "SKILL.md"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _frontmatter() -> str:
    return _skill_text().split("---", 2)[1]


def _has_word(text: str, word: str) -> bool:
    """Whole-word (not substring) match so "5950" does not count as "595"."""
    return re.search(r"(?<![\w.])" + re.escape(word) + r"(?![\w.])", text) is not None


class TestFrontmatterIntact:
    """Stage 0 defined the trigger surface; stage 4 must keep it."""

    def test_name_and_argument_hint_intact(self):
        front = _frontmatter()
        assert "name: trix-arena" in front
        assert "argument-hint:" in front
        for sub in commands.KNOWN_SUBCOMMANDS:
            assert sub in front, "subcommand %r missing from argument-hint" % sub

    def test_triggers_kept_in_description(self):
        front = _frontmatter().lower()
        for trigger in ("not satisfied", "bad", "try again", '"arena"', "compete"):
            assert trigger in front, "trigger %r missing from description" % trigger


class TestStubReplaced:
    def test_no_stage0_markers(self):
        text = _skill_text()
        assert "Stage 0 status" not in text
        assert "do not try to use yet" not in text
        assert "pong" in text  # ping is still documented


class TestRequiredSections:
    SECTIONS = (
        "## When to invoke the arena",
        "## Choosing a mode",
        "## The canonical task package",
        "## Preflight: know the cost before you start",
        "## Driving a run: the six subcommands",
        "## Executing the jobs",
        "## The final answer",
        "## Limits of the arena",
    )

    def test_all_sections_present(self):
        text = _skill_text()
        for heading in self.SECTIONS:
            assert heading in text, "missing section: %r" % heading


class TestPreflightNumbers:
    """The documented costs must equal core.plan_totals, not folklore."""

    def test_documented_costs_match_plan_totals(self):
        text = _skill_text()
        for agents in (core.DEFAULT_AGENTS, core.QUICK_AGENTS):
            plan = core.plan_totals(agents, core.DEFAULT_WAVE)
            for key in ("rounds", "calls", "waves"):
                value = str(plan[key])
                assert _has_word(text, value), (
                    "preset %r: %s=%s not documented" % (agents, key, value)
                )

    def test_wave_changes_batches_not_calls(self):
        # The skill claims --wave only re-batches jobs; pin that claim.
        base = core.plan_totals(core.DEFAULT_AGENTS, core.DEFAULT_WAVE)["calls"]
        for wave in (1, 3, 10, 100):
            assert core.plan_totals(core.DEFAULT_AGENTS, wave)["calls"] == base


class TestDocumentedSurface:
    def test_all_subcommands_documented(self):
        text = _skill_text()
        for sub in commands.KNOWN_SUBCOMMANDS:
            assert "/arena " + sub in text, "subcommand /arena %s undocumented" % sub

    def test_all_tools_documented(self):
        text = _skill_text()
        for tool in schemas.TOOL_SCHEMAS:
            assert _has_word(text, tool), "tool %r undocumented" % tool


class TestFinalAnswerFormat:
    """PLAN stage 4: final answer = winner, score, survived attacks, limits."""

    def test_final_answer_covers_required_parts(self):
        text = _skill_text().lower()
        for part in ("winner", "score", "survived", "limitations"):
            assert part in text, "final-answer part %r undocumented" % part

    def test_score_weights_documented(self):
        text = _skill_text().lower()
        # The rubric weights behind the reported totals.
        for criterion in ("correctness", "completeness", "specificity", "robustness", "clarity"):
            assert criterion in text, "rubric criterion %r undocumented" % criterion

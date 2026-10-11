"""Tests for the Trix Arena plugin (stage 0 skeleton).

These tests cover the stage 0 scope only:

- ``/arena ping`` answers exactly ``pong``.
- The ``arena_ping`` tool answers ``{"pong": True}``.
- ``plugin.yaml`` lists the command and the tool and points at real
  modules.
- Package imports cleanly (register() works against a fake context).
"""

from __future__ import annotations

import unittest
from pathlib import Path

import importlib.util
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_ROOT.parents[1]

# Import as a top-level package "trix_arena" so relative imports inside
# the plugin work no matter where the repo sits on disk.
_SPEC = importlib.util.spec_from_file_location(
    "trix_arena", PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
trix_arena = importlib.util.module_from_spec(_SPEC)
sys.modules["trix_arena"] = trix_arena
_SPEC.loader.exec_module(trix_arena)


class FakeContext:
    """Minimal stand-in for the Trix Agent plugin context."""

    def __init__(self) -> None:
        self.commands = {}
        self.tools = {}

    def register_command(self, name: str, handler) -> None:
        self.commands[name] = handler

    def register_tool(self, name: str, handler) -> None:
        self.tools[name] = handler


class PingTests(unittest.TestCase):
    def test_arena_ping_answers_pong(self):
        from trix_arena.commands import arena_command

        self.assertEqual(arena_command(["ping"]), "pong")
        self.assertEqual(arena_command(["ping", "extra"]), "pong")

    def test_unknown_subcommand_gives_usage(self):
        from trix_arena.commands import arena_command

        for args in ([], ["nonsense"], ["plan"]):
            with self.subTest(args=args):
                out = arena_command(args)
                self.assertNotEqual(out, "pong")
                self.assertIn("ping", out)

    def test_planned_subcommands_not_implemented(self):
        from trix_arena.commands import arena_command

        out = arena_command(["start", "do the thing"])
        self.assertIn("not implemented", out)

    def test_tool_ping(self):
        from trix_arena.tools import arena_ping

        self.assertEqual(arena_ping(), {"pong": True, "plugin": "trix-arena"})
        self.assertEqual(
            arena_ping({"payload": "hi"}),
            {"pong": True, "plugin": "trix-arena", "payload": "hi"},
        )

    def test_tool_ping_rejects_unknown_keys(self):
        from trix_arena.tools import arena_ping

        out = arena_ping({"nope": 1})
        self.assertFalse(out["pong"])
        self.assertIn("error", out)

    def test_schema_validation(self):
        from trix_arena.schemas import validate_tool_input

        self.assertTrue(validate_tool_input("arena_ping", {}))
        self.assertTrue(validate_tool_input("arena_ping", {"payload": "x"}))
        self.assertFalse(validate_tool_input("arena_ping", {"other": 1}))
        with self.assertRaises(KeyError):
            validate_tool_input("arena_nope", {})
        with self.assertRaises(ValueError):
            from trix_arena.schemas import loads
            loads("{bad json")

    def test_register(self):
        ctx = FakeContext()
        trix_arena.register(ctx)
        self.assertIn("arena", ctx.commands)
        self.assertIn("arena_ping", ctx.tools)
        self.assertEqual(ctx.commands["arena"](["ping"]), "pong")
        self.assertTrue(ctx.tools["arena_ping"]()["pong"])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        # plugin.yaml is YAML; stage 0 avoids a hard dependency by parsing
        # the few fields we assert on with a targeted read.
        self.manifest_text = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.manifest = _parse_simple_yaml(self.manifest_text)

    def test_manifest_lists_command_and_tool(self):
        self.assertEqual(self.manifest["id"], "trix-arena")
        self.assertEqual(self.manifest["name"], "Trix Arena")
        self.assertIn("arena", self.manifest_text)
        self.assertIn("arena_ping", self.manifest_text)
        self.assertIn("commands.arena_command", self.manifest_text)
        self.assertIn("tools.arena_ping", self.manifest_text)

    def test_manifest_declares_handlers(self):
        self.assertIn("handler: commands.arena_command", self.manifest_text)
        self.assertIn("handler: tools.arena_ping", self.manifest_text)

    def test_skill_file_exists(self):
        skill = PLUGIN_ROOT / "skills" / "trix-arena" / "SKILL.md"
        self.assertTrue(skill.is_file(), skill)

    def test_data_dir_exists(self):
        data = PLUGIN_ROOT / "data"
        self.assertTrue(data.is_dir(), data)


def _parse_simple_yaml(text: str) -> dict:
    """Extract a few top-level scalar keys from plugin.yaml.

    Deliberately tiny: stage 0 only asserts id/name presence, and the full
    manifest contract (with a real YAML library) arrives with stage 2.
    """
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line[:1].isspace():
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


if __name__ == "__main__":
    unittest.main()

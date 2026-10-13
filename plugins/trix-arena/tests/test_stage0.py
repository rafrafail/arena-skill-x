"""Stage 0 plugin skeleton tests: /arena ping answers pong."""

from __future__ import annotations

import os
import tempfile
import unittest

from trix_arena.schemas import validate_tool_input


class PingTests(unittest.TestCase):
    def test_arena_ping_answers_pong(self):
        from trix_arena.commands import arena_command

        self.assertEqual(arena_command(["ping"]), "pong")
        self.assertEqual(arena_command(["ping", "extra"]), "pong")

    def test_unknown_subcommand_gives_usage(self):
        from trix_arena.commands import arena_command

        for args in ([], ["nonsense"]):
            with self.subTest(args=args):
                out = arena_command(args)
                self.assertNotEqual(out, "pong")
                self.assertIn("ping", out)

    def test_no_known_subcommand_reports_not_implemented(self):
        from trix_arena.commands import arena_command

        # Every registered subcommand is implemented since stage 2; none of
        # them may answer with the old "not implemented" stub. The whole
        # loop runs in a tmp cwd because "start" really creates a run.
        with tempfile.TemporaryDirectory() as tmp:
            prev = os.getcwd()
            os.chdir(tmp)
            try:
                for args in ([], ["nonsense"], ["ping"], ["plan"],
                             ["start", "do the thing"], ["status"],
                             ["result"], ["cancel"]):
                    with self.subTest(args=args):
                        out = arena_command(args)
                        self.assertNotIn("not implemented", out)
            finally:
                os.chdir(prev)

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
        self.assertTrue(validate_tool_input("arena_ping", {}))
        self.assertTrue(validate_tool_input("arena_ping", {"payload": "x"}))
        self.assertFalse(validate_tool_input("arena_ping", {"other": 1}))
        with self.assertRaises(KeyError):
            validate_tool_input("arena_nope", {})

    def test_json_loads_rejects_garbage(self):
        from trix_arena.schemas import loads

        with self.assertRaises(ValueError):
            loads("{bad json")


class FakeContext:
    def __init__(self) -> None:
        self.commands = {}
        self.tools = {}

    def register_command(self, name: str, handler) -> None:
        self.commands[name] = handler

    def register_tool(self, name: str, handler) -> None:
        self.tools[name] = handler


class RegisterTests(unittest.TestCase):
    def test_register_wires_command_and_tool(self):
        import trix_arena

        ctx = FakeContext()
        trix_arena.register(ctx)
        self.assertIn("arena", ctx.commands)
        self.assertIn("arena_ping", ctx.tools)
        self.assertEqual(ctx.commands["arena"](["ping"]), "pong")
        self.assertTrue(ctx.tools["arena_ping"]()["pong"])


if __name__ == "__main__":
    unittest.main()

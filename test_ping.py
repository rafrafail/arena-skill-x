#!/usr/bin/env python3
"""Quick test of /arena ping command"""

import sys
from pathlib import Path

# Add plugins directory to path
PLUGIN_ROOT = Path(__file__).resolve().parent / "plugins" / "trix-arena"
sys.path.insert(0, str(PLUGIN_ROOT))

from trix_arena.commands import arena_command

# Test /arena ping
result = arena_command(["ping"])
print(f"/arena ping -> '{result}'")
assert result == "pong", f"Expected 'pong', got '{result}'"

# Test /arena ping with extra args
result = arena_command(["ping", "extra", "args"])
print(f"/arena ping extra -> '{result}'")
assert result == "pong", f"Expected 'pong', got '{result}'"

print("SUCCESS: /arena ping returns 'pong'")
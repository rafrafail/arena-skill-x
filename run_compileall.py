#!/usr/bin/env python3
"""Run python3 -m compileall on the plugins directory.

This script emulates the gate check from PLAN.md stage 0:
    python3 -m compileall plugins
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

if __name__ == "__main__":
    result = subprocess.run(
        [sys.executable, "-m", "compileall", str(PLUGINS_DIR)],
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    sys.exit(result.returncode)
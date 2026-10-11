import compileall
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

print("repo root:", REPO_ROOT)
print("plugins dir exists:", PLUGINS_DIR.is_dir())
for p in sorted(PLUGINS_DIR.rglob("*")):
    print("  ", p.relative_to(REPO_ROOT))

ok = compileall.compile_dir(str(PLUGINS_DIR), quiet=1)
print("compileall result:", ok)
sys.exit(0 if ok else 1)
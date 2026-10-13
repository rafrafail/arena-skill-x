"""Pytest configuration: expose the plugin as top-level package ``trix_arena``.

The plugin lives at ``plugins/trix-arena/`` inside the repository, but its
modules refer to each other as ``trix_arena.*`` (and ``plugin.yaml`` expects
that import name too). This conftest registers the plugin directory as a
top-level package so test modules can simply ``import trix_arena``.

``tests/__init__.py`` calls the same function, so non-pytest runners
(``python -m unittest``) still work; the registration is idempotent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def ensure_trix_arena_importable():
    """Register the plugin directory as the top-level package ``trix_arena``."""
    existing = sys.modules.get("trix_arena")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "trix_arena",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["trix_arena"] = module
    spec.loader.exec_module(module)
    return module


ensure_trix_arena_importable()

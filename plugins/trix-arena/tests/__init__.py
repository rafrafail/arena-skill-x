"""Tests for the Trix Arena plugin.

The ``trix_arena`` import bootstrap lives in ``conftest.py``
(``ensure_trix_arena_importable``). It runs here as well so that
non-pytest runners (``python -m unittest``) still import the plugin
correctly; the bootstrap itself is idempotent.
"""

from __future__ import annotations

from .conftest import ensure_trix_arena_importable

ensure_trix_arena_importable()

"""Shared tournament core for Trix Arena.

This subpackage will hold the deterministic tournament engine extracted
from the upstream skill (``skills/arena/bracket.py``): card dealing,
bracket pairing and byes, scoring, JSON state management and migrations.

Upstream files under ``skills/`` at the repository root are never
modified; everything here is a fresh, path-safe implementation built in
later stages (characterization tests first).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

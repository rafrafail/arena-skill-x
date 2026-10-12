#!/usr/bin/env python3
"""Shared tournament core for Trix Arena.

This subpackage holds the deterministic tournament engine extracted
from the upstream skill (``skills/arena/bracket.py``): card dealing,
bracket pairing and byes, scoring, JSON state management and migrations.

Upstream files under ``skills/`` at the repository root are never
modified; everything here is a fresh, path-safe implementation with
relative-path state IO and a versioned state schema.
"""

from .core import (
    # Constants
    DEFAULT_AGENTS,
    QUICK_AGENTS,
    DEFAULT_WAVE,
    ROOT,
    LATEST,
    STATE_FILE,
    PHASES,
    CALLS_PER_MATCH,
    WEIGHTS,
    CURRENT_STATE_SCHEMA_VERSION,
    # Errors
    ArenaError,
    # Strategies / Cards
    load_strategies,
    combo_count,
    deal,
    agent_ids,
    bracket_sizes,
    # State
    new_state,
    current_round,
    record,
    advance,
    rounds_played,
    alive_ids,
    # Scoring
    weighted_total,
    extract_json,
    decide,
    collect,
    # Jobs
    phase_jobs,
    missing_jobs,
    # State I/O + Migration
    save_state,
    load_state,
    migrate,
    ensure_current_schema,
    # Plan
    plan_rows,
    plan_totals,
)

__all__ = [
    "DEFAULT_AGENTS",
    "QUICK_AGENTS",
    "DEFAULT_WAVE",
    "ROOT",
    "LATEST",
    "STATE_FILE",
    "PHASES",
    "CALLS_PER_MATCH",
    "WEIGHTS",
    "CURRENT_STATE_SCHEMA_VERSION",
    "ArenaError",
    "load_strategies",
    "combo_count",
    "deal",
    "agent_ids",
    "bracket_sizes",
    "new_state",
    "current_round",
    "record",
    "advance",
    "rounds_played",
    "alive_ids",
    "weighted_total",
    "extract_json",
    "decide",
    "collect",
    "phase_jobs",
    "missing_jobs",
    "save_state",
    "load_state",
    "migrate",
    "ensure_current_schema",
    "plan_rows",
    "plan_totals",
    "__version__",
]

__version__ = "0.2.0"
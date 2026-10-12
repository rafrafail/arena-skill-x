#!/usr/bin/env python3
"""Core tournament engine for Trix Arena.

Extracted from upstream skills/arena/bracket.py with:
- Relative paths only (caller provides base directory)
- state_schema_version field + migration interface
- Atomic save semantics (write .tmp then os.replace)
- No absolute paths, no cwd writes by default
- stdlib only

This is the canonical tournament core for the plugin.
"""
import json
import math
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# ---- Constants (match upstream exactly) ----
DEFAULT_AGENTS = 100
QUICK_AGENTS = 16
DEFAULT_WAVE = 10
ROOT = ".arena"
LATEST = "LATEST"
STATE_FILE = "arena.json"
PHASES = ("spawn", "attack", "defend", "judge", "final")
CALLS_PER_MATCH = 5

WEIGHTS = (
    ("correctness", 30),
    ("completeness", 25),
    ("specificity", 15),
    ("robustness", 20),
    ("clarity", 10),
)

CURRENT_STATE_SCHEMA_VERSION = 2


class ArenaError(Exception):
    """Tournament engine error."""
    pass


# ------------------------------------------------------------ Strategies / Cards

def load_strategies(path: Optional[str] = None) -> Dict[str, List[Dict]]:
    """Load and validate strategies.json from the given path or default location."""
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "..", "..", "..", "skills", "arena", "strategies.json")
        path = os.path.normpath(path)

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    for key in ("reasoning", "workflows", "strategies"):
        items = data.get(key) or []
        if not items:
            raise ArenaError(f"strategies.json has no '{key}' entries")
        ids = [it["id"] for it in items]
        if len(set(ids)) != len(ids):
            raise ArenaError(f"strategies.json has a duplicate id in '{key}'")
        for it in items:
            if not it.get("name") or not it.get("how"):
                raise ArenaError(f"strategies.json entry '{it['id']}' needs a name and a how")
    return data


def combo_count(data: Dict[str, List[Dict]]) -> int:
    """Total distinct cards available."""
    return len(data["reasoning"]) * len(data["workflows"]) * len(data["strategies"])


def _edge_colour(edges: List[Tuple[int, int]], k: int) -> List[int]:
    """Properly edge-colour a bipartite multigraph with k colours, then balance it.

    Konig's theorem guarantees a proper colouring for bipartite graphs.
    De Werra's swaps balance colour class sizes.
    """
    colour = [None] * len(edges)
    at = ({}, {})  # side -> node -> colour -> edge_index

    def slot(side: int, node: int) -> Dict[int, int]:
        return at[side].setdefault(node, {})

    def put(e: int, c: int) -> None:
        u, v = edges[e]
        colour[e] = c
        slot(0, u)[c] = e
        slot(1, v)[c] = e

    def take(e: int) -> None:
        u, v = edges[e]
        c = colour[e]
        del slot(0, u)[c]
        del slot(1, v)[c]

    def walk(side: int, node: int, first: int, other: int) -> List[int]:
        path, c = [], first
        while True:
            e = slot(side, node).get(c)
            if e is None or e in path:
                return path
            path.append(e)
            node = edges[e][1 - side]
            side = 1 - side
            c = other if c == first else first

    def swap(path: List[int], a: int, b: int) -> None:
        new = {e: (b if colour[e] == a else a) for e in path}
        for e in path:
            take(e)
        for e in path:
            put(e, new[e])

    # Proper colouring
    for e, (u, v) in enumerate(edges):
        free_u = [c for c in range(k) if c not in slot(0, u)]
        free_v = set(c for c in range(k) if c not in slot(1, v))
        c = next((c for c in free_u if c in free_v), None)
        if c is None:
            a, b = free_u[0], min(free_v)
            swap(walk(1, v, a, b), a, b)
            c = a
        put(e, c)

    # Balance colour classes
    while True:
        count = [0] * k
        for c in colour:
            count[c] += 1
        hi = max(range(k), key=lambda c: (count[c], -c))
        lo = min(range(k), key=lambda c: (count[c], c))
        if count[hi] - count[lo] <= 1:
            return colour
        swapped = False
        for side in (0, 1):
            for node in sorted(at[side]):
                s = at[side][node]
                if hi in s and lo not in s:
                    path = walk(side, node, hi, lo)
                    if sum(colour[e] == hi for e in path) > sum(colour[e] == lo for e in path):
                        swap(path, hi, lo)
                        swapped = True
                        break
            if swapped:
                break
        if not swapped:
            raise ArenaError("could not balance the strategy cards")


def deal(n: int, seed: str, data: Dict[str, List[Dict]]) -> List[Tuple[int, int, int]]:
    """Deal n distinct (reasoning, workflow, strategy) index triples, one per agent.

    Deterministic for a given seed and strategies.json. Guarantees:
      - no card is dealt twice
      - every reasoning mode, workflow and strategy is dealt as evenly as possible
        (any two counts differ by at most 1)
      - while every reasoning mode and every workflow has no more agents than there
        are strategies (up to 144 agents): no two agents share two dimensions.
    """
    R, W, S = len(data["reasoning"]), len(data["workflows"]), len(data["strategies"])
    total = R * W * S
    if n < 1 or n > total:
        raise ArenaError(f"can deal between 1 and {total} cards, not {n}")

    rng = random.Random(f"arena-deal:{seed}")
    pr, pw, ps = list(range(R)), list(range(W)), list(range(S))
    rng.shuffle(pr)
    rng.shuffle(pw)
    rng.shuffle(ps)

    lcm = R * W // math.gcd(R, W)
    cells = [(i % R, (i + i // lcm) % W) for i in range(n)]

    deg_r = max(sum(1 for r, _ in cells if r == x) for x in range(R))
    deg_w = max(sum(1 for _, w in cells if w == x) for x in range(W))

    if deg_r <= S and deg_w <= S:
        order = list(range(n))
        rng.shuffle(order)
        colours = _edge_colour([cells[i] for i in order], S)
        strategy_of = {order[j]: colours[j] for j in range(n)}
        triples = [(r, w, strategy_of[i]) for i, (r, w) in enumerate(cells)]
    else:
        count, pairs_rs, pairs_ws, used, triples = [0] * S, {}, {}, set(), []
        for r, w in cells:
            low = min(count)
            s = min(
                (c for c in range(S) if (r, w, c) not in used),
                key=lambda c: (count[c] - low, pairs_rs.get((r, c), 0), pairs_ws.get((w, c), 0), c)
            )
            used.add((r, w, s))
            count[s] += 1
            pairs_rs[(r, s)] = pairs_rs.get((r, s), 0) + 1
            pairs_ws[(w, s)] = pairs_ws.get((w, s), 0) + 1
            triples.append((r, w, s))

    dealt = [(pr[r], pw[w], ps[s]) for r, w, s in triples]
    rng.shuffle(dealt)
    return dealt


def agent_ids(n: int) -> List[str]:
    """Generate zero-padded agent IDs: a001, a002, ..."""
    width = max(3, len(str(n)))
    return [f"a{i:0{width}d}" for i in range(1, n + 1)]


def bracket_sizes(n: int) -> List[int]:
    """Alive count at the start of each round, ending at 1.
    100 -> 50 -> 25 -> 13 -> 7 -> 4 -> 2 -> 1
    """
    sizes = [n]
    while sizes[-1] > 1:
        sizes.append((sizes[-1] + 1) // 2)
    return sizes


# ------------------------------------------------------------ State

def new_state(
    n: int,
    seed: str,
    data: Dict[str, List[Dict]],
    run_dir: str,
    wave: int = DEFAULT_WAVE,
    has_baseline: bool = False,
) -> Dict[str, Any]:
    """Create a new tournament state. run_dir is the caller-provided base directory.

    The state uses relative paths under run_dir (no os.path.abspath).
    """
    # Keep run_dir as provided (relative or absolute) - caller controls normalization
    ids = agent_ids(n)
    agents = {}
    for aid, (r, w, s) in zip(ids, deal(n, seed, data)):
        agents[aid] = {
            "card": {
                "reasoning": dict(data["reasoning"][r]),
                "workflow": dict(data["workflows"][w]),
                "strategy": dict(data["strategies"][s]),
            },
            "solution": _spawn_out_rel(aid),
            "alive": True,
            "eliminated_in": None,
            "eliminated_by": None,
            "byes": 0,
        }

    state = {
        "state_schema_version": CURRENT_STATE_SCHEMA_VERSION,
        "version": 1,  # upstream-compatible field
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed": seed,
        "agents_n": n,
        "cards_available": combo_count(data),
        "wave": wave,
        "dir": run_dir,  # absolute (normalized), but migration will relativize
        "has_baseline": bool(has_baseline),
        "agents": agents,
        "rounds": [],
        "champion": None,
        "final": None,
    }
    _open_round(state, ids)
    return state


def _spawn_out(run_dir: str, aid: str) -> str:
    return os.path.join(run_dir, "r0", f"{aid}.md")


def _spawn_out_rel(aid: str) -> str:
    """Relative path for state storage."""
    return os.path.join("r0", f"{aid}.md")


def _attack_out(run_dir: str, rnd: int, mid: str, attacker: str) -> str:
    return os.path.join(run_dir, f"r{rnd}", f"{mid}.{attacker}.attack.md")


def _defense_out(run_dir: str, rnd: int, mid: str, aid: str) -> str:
    return os.path.join(run_dir, f"r{rnd}", f"{mid}.{aid}.defense.md")


def _revised_out(run_dir: str, rnd: int, mid: str, aid: str) -> str:
    return os.path.join(run_dir, f"r{rnd}", f"{mid}.{aid}.solution.md")


def _revised_out_rel(rnd: int, mid: str, aid: str) -> str:
    """Relative path for state storage."""
    return os.path.join(f"r{rnd}", f"{mid}.{aid}.solution.md")


def _verdict_out(run_dir: str, rnd: int, mid: str) -> str:
    return os.path.join(run_dir, f"r{rnd}", f"{mid}.verdict.json")


def _final_out(run_dir: str) -> str:
    return os.path.join(run_dir, "final.verdict.json")


def _prompt_path(run_dir: str, rnd: Optional[int], job_id: str) -> str:
    sub = "final" if rnd is None else f"r{rnd}"
    return os.path.join(run_dir, "prompts", sub, f"{job_id}.md")


def _has_output(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _pair_round(state: Dict[str, Any], rnd: int, alive: List[str]) -> Tuple[List[Dict], Optional[str]]:
    """Seeded pairing. Odd pool -> bye to fewest-bye agent. Prefers different reasoning. Sides 50/50."""
    agents = state["agents"]
    rng = random.Random(f"arena-pair:{state['seed']}:{rnd}")
    pool = sorted(alive)
    rng.shuffle(pool)

    bye = None
    if len(pool) % 2:
        fewest = min(agents[a]["byes"] for a in pool)
        bye = next(a for a in pool if agents[a]["byes"] == fewest)
        pool.remove(bye)

    width = max(2, len(str(len(pool) // 2)))
    matches = []
    while pool:
        a = pool.pop(0)
        mode = agents[a]["card"]["reasoning"]["id"]
        j = next((k for k, b in enumerate(pool) if agents[b]["card"]["reasoning"]["id"] != mode), 0)
        b = pool.pop(j)
        if rng.random() < 0.5:
            a, b = b, a
        matches.append({
            "id": f"r{rnd}-m{len(matches) + 1:0{width}d}",
            "a": a, "b": b,
            "winner": None, "loser": None, "reason": None,
            "survived": [], "scores": None, "source": None,
        })
    return matches, bye


def _open_round(state: Dict[str, Any], alive: List[str]) -> Optional[Dict]:
    """Open the next round, or crown the champion when one agent is left."""
    alive = sorted(alive)
    if len(alive) == 1:
        state["champion"] = alive[0]
        if state.get("has_baseline") and not state.get("final"):
            rng = random.Random(f"arena-final:{state['seed']}")
            x = "champion" if rng.random() < 0.5 else "baseline"
            state["final"] = {"X": x, "Y": "baseline" if x == "champion" else "champion", "result": None}
        return None

    rnd = len(state["rounds"]) + 1
    matches, bye = _pair_round(state, rnd, alive)
    rd = {"n": rnd, "alive": alive, "matches": matches, "bye": bye, "closed": False}
    state["rounds"].append(rd)
    return rd


def current_round(state: Dict[str, Any]) -> Optional[Dict]:
    """Get the current open round, or None if champion crowned or no rounds."""
    if state["champion"] or not state["rounds"]:
        return None
    rd = state["rounds"][-1]
    return None if rd["closed"] else rd


def _norm_agent(state: Dict[str, Any], raw: str) -> str:
    """Normalize agent identifier to canonical form."""
    raw = str(raw).strip().lower()
    if raw in state["agents"]:
        return raw
    digits = raw.lstrip("a")
    if digits.isdigit():
        for aid in state["agents"]:
            if int(aid[1:]) == int(digits):
                return aid
    raise ArenaError(f"no agent called '{raw}'")


def _find_match(state: Dict[str, Any], mid: str) -> Tuple[Dict, Dict]:
    """Find match by ID in current round."""
    rd = current_round(state)
    if rd is None:
        raise ArenaError(
            "no round is open" + (" (the arena has a champion)" if state["champion"] else "")
        )
    raw = str(mid).strip().lower()
    for m in rd["matches"]:
        if m["id"] == raw:
            return rd, m
    tail = raw.split("-m")[-1].lstrip("m")
    if tail.isdigit():
        for m in rd["matches"]:
            if int(m["id"].split("-m")[-1]) == int(tail):
                return rd, m
    raise ArenaError(f"no match '{mid}' in round {rd['n']} (run pairings)")


def record(
    state: Dict[str, Any],
    mid: str,
    winner: str,
    reason: Optional[str] = None,
    survived: Optional[List[str]] = None,
    scores: Optional[Dict] = None,
    source: str = "manual",
) -> Tuple[Dict, Optional[str]]:
    """Record a match result. Returns (updated_match, previous_winner)."""
    rd, m = _find_match(state, mid)
    winner = _norm_agent(state, winner)
    if winner not in (m["a"], m["b"]):
        raise ArenaError(f"{winner} is not in {m['id']} ({m['a']} vs {m['b']})")
    previous = m["winner"]
    m["winner"] = winner
    m["loser"] = m["b"] if winner == m["a"] else m["a"]
    m["reason"] = reason or None
    m["survived"] = list(survived or [])
    m["scores"] = scores
    m["source"] = source
    return m, previous


def advance(state: Dict[str, Any]) -> List[str]:
    """Close the current round: eliminate losers, carry winners' solutions, bye survives, open next."""
    rd = current_round(state)
    if rd is None:
        raise ArenaError(
            "no round is open" + (" (the arena has a champion)" if state["champion"] else "")
        )

    unrecorded = [m["id"] for m in rd["matches"] if not m["winner"]]
    if unrecorded:
        raise ArenaError(
            f"round {rd['n']} has {len(unrecorded)} unrecorded match(es): {', '.join(unrecorded)}"
        )

    survivors = []
    run_dir = state["dir"]
    for m in rd["matches"]:
        loser = state["agents"][m["loser"]]
        loser["alive"] = False
        loser["eliminated_in"] = rd["n"]
        loser["eliminated_by"] = m["winner"]
        # Carry forward revised solutions if they exist
        for aid in (m["a"], m["b"]):
            rev = _revised_out(run_dir, rd["n"], m["id"], aid)
            if _has_output(rev):
                state["agents"][aid]["solution"] = _revised_out_rel(rd["n"], m["id"], aid)
        survivors.append(m["winner"])

    if rd["bye"]:
        state["agents"][rd["bye"]]["byes"] += 1
        survivors.append(rd["bye"])

    rd["closed"] = True
    _open_round(state, survivors)
    return survivors


def rounds_played(state: Dict[str, Any]) -> int:
    return sum(1 for rd in state["rounds"] if rd["closed"])


def alive_ids(state: Dict[str, Any]) -> List[str]:
    return sorted(a for a, v in state["agents"].items() if v["alive"])


# ------------------------------------------------------------ Scoring / Verdicts

def weighted_total(scores: Dict) -> Optional[float]:
    """0-100 from five 0-10 scores, weighted per rubric. Returns None if any missing/invalid."""
    if not isinstance(scores, dict):
        return None
    total = 0.0
    for key, weight in WEIGHTS:
        v = scores.get(key)
        if isinstance(v, bool):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        total += max(0.0, min(10.0, v)) * weight
    return round(total / 10.0, 2)


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def extract_json(text: str) -> Optional[Dict]:
    """Extract JSON from text, handling code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            return None
    return None


def decide(verdict: Dict, a: str, b: str) -> Tuple[str, Dict[str, Optional[float]], str]:
    """Apply rubric arithmetic to a judge's verdict.

    Returns (winner, {a: total, b: total}, note).
    """
    if not isinstance(verdict, dict):
        raise ArenaError("verdict is not a JSON object")

    raw_scores = verdict.get("scores") or {}
    scores = {str(k).strip().lower(): v for k, v in raw_scores.items()} if isinstance(raw_scores, dict) else {}
    sa, sb = scores.get(a), scores.get(b)

    pick_raw = str(verdict.get("winner") or "").strip().lower()
    in_a = re.search(rf"\b{re.escape(a)}\b", pick_raw) is not None
    in_b = re.search(rf"\b{re.escape(b)}\b", pick_raw) is not None
    pick = a if (in_a and not in_b) else b if (in_b and not in_a) else None

    ta, tb = weighted_total(sa), weighted_total(sb)
    totals = {a: ta, b: tb}

    if ta is None or tb is None:
        if pick:
            return pick, totals, "scores incomplete, used the judge's pick"
        raise ArenaError("verdict has neither complete scores nor a clear winner")

    fa, fb = _truthy(sa.get("fatal")), _truthy(sb.get("fatal"))
    if fa != fb:
        winner = b if fa else a
    elif ta != tb:
        winner = a if ta > tb else b
    else:
        standing = verdict.get("standing") if isinstance(verdict.get("standing"), dict) else {}
        standing = {str(k).strip().lower(): v for k, v in standing.items()}
        na = len(standing.get(a) or []) if isinstance(standing.get(a), list) else None
        nb = len(standing.get(b) or []) if isinstance(standing.get(b), list) else None
        ca, cb = float(sa.get("correctness", 0)), float(sb.get("correctness", 0))
        if na is not None and nb is not None and na != nb:
            winner = a if na < nb else b
        elif ca != cb:
            winner = a if ca > cb else b
        elif pick:
            winner = pick
        else:
            raise ArenaError("exact tie and the judge did not pick a winner")

    note = ""
    if pick and pick != winner:
        note = f"judge picked {pick} but its own scores favour {winner}, so the scores win"
    return winner, totals, note


def collect(state: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Record every verdict the judges have written for the open round (or final).
    Returns list of (match_id, status, note).
    Unreadable verdicts are moved to .unreadable and reappear as missing.
    """
    run_dir = state["dir"]
    results = []

    if state["champion"]:
        fin = state.get("final")
        if not fin or fin.get("result"):
            return results
        path = _final_out(run_dir)
        if not _has_output(path):
            return [("final", "missing", "")]
        with open(path, encoding="utf-8", errors="replace") as fh:
            v = extract_json(fh.read())
        try:
            if isinstance(v, dict) and isinstance(v.get("scores"), dict):
                v = dict(v, scores={str(k).strip().lower(): s for k, s in v["scores"].items()})
            w, totals, note = decide(v, "x", "y")
        except ArenaError as e:
            os.replace(path, path + ".unreadable")
            return [("final", "unreadable", str(e))]
        better = fin[w.upper()]
        fin["result"] = {
            "better": better,
            "champion_total": totals["x" if fin["X"] == "champion" else "y"],
            "baseline_total": totals["x" if fin["X"] == "baseline" else "y"],
            "reason": str(v.get("reason") or "")[:400],
            "fixed": [str(x) for x in (v.get("fixed") or [])][:10] if isinstance(v.get("fixed"), list) else [],
        }
        return [("final", "recorded", f"{better} is better. {note}".strip())]

    rd = current_round(state)
    if rd is None:
        return results

    for m in rd["matches"]:
        if m["winner"]:
            continue
        path = _verdict_out(run_dir, rd["n"], m["id"])
        if not _has_output(path):
            results.append((m["id"], "missing", ""))
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            v = extract_json(fh.read())
        try:
            winner, totals, note = decide(v, m["a"], m["b"])
        except ArenaError as e:
            os.replace(path, path + ".unreadable")
            results.append((m["id"], "unreadable", str(e)))
            continue
        survived = v.get("survived") if isinstance(v.get("survived"), list) else []
        record(
            state, m["id"], winner,
            reason=str(v.get("reason") or "")[:400],
            survived=[str(x) for x in survived][:10],
            scores=totals, source="judge"
        )
        loser = m["loser"]
        def _fmt(x): return "?" if x is None else f"{x:g}"
        line = f"{winner} beat {loser}, {_fmt(totals[winner])} to {_fmt(totals[loser])}"
        results.append((m["id"], "recorded", line + (f". {note}" if note else "")))
    return results


# ------------------------------------------------------------ Jobs / Prompts

def phase_jobs(state: Dict[str, Any], phase: str) -> List[Dict]:
    """Every sub-agent job for a phase, with prompt file and required outputs."""
    if phase not in PHASES:
        raise ArenaError(f"unknown phase '{phase}' (one of: {', '.join(PHASES)})")

    run_dir = state["dir"]
    jobs = []

    if phase == "spawn":
        for aid in sorted(state["agents"]):
            jobs.append({
                "id": f"{aid}.spawn", "kind": "competitor", "round": 0, "agent": aid,
                "outputs": [_spawn_out(run_dir, aid)]
            })
    elif phase == "final":
        fin = state.get("final")
        if state["champion"] and fin and not fin.get("result"):
            jobs.append({
                "id": "final.judge", "kind": "final", "round": None,
                "outputs": [_final_out(run_dir)]
            })
    else:
        rd = current_round(state)
        if rd is None:
            return []
        n = rd["n"]
        for m in rd["matches"]:
            if m["winner"]:
                continue
            a, b = m["a"], m["b"]
            if phase == "attack":
                for me, opp in ((a, b), (b, a)):
                    jobs.append({
                        "id": f"{m['id']}.{me}.attack", "kind": "attacker", "round": n,
                        "agent": me, "opponent": opp, "match": m["id"],
                        "outputs": [_attack_out(run_dir, n, m["id"], me)]
                    })
            elif phase == "defend":
                for me, opp in ((a, b), (b, a)):
                    jobs.append({
                        "id": f"{m['id']}.{me}.defend", "kind": "defender", "round": n,
                        "agent": me, "opponent": opp, "match": m["id"],
                        "outputs": [
                            _defense_out(run_dir, n, m["id"], me),
                            _revised_out(run_dir, n, m["id"], me)
                        ]
                    })
            else:  # judge
                jobs.append({
                    "id": f"{m['id']}.judge", "kind": "judge", "round": n,
                    "match": m["id"], "a": a, "b": b,
                    "outputs": [_verdict_out(run_dir, n, m["id"])]
                })

    for j in jobs:
        j["prompt"] = _prompt_path(run_dir, j["round"], j["id"])
    return jobs


def missing_jobs(jobs: List[Dict]) -> List[Dict]:
    return [j for j in jobs if not all(_has_output(p) for p in j["outputs"])]


# ------------------------------------------------------------ State I/O + Migration

def save_state(state: Dict[str, Any], base_dir: Optional[str] = None) -> None:
    """Atomic save: write .tmp then os.replace.

    If base_dir is provided, state["dir"] is stored as a relative path under base_dir.
    """
    run_dir = state["dir"]
    if base_dir:
        try:
            run_dir = os.path.relpath(run_dir, base_dir)
        except ValueError:
            # Different drives on Windows, keep absolute
            pass

    # We need to write to the actual directory, so use the original absolute path
    actual_dir = state["dir"]
    path = os.path.join(actual_dir, STATE_FILE)
    tmp = path + ".tmp"

    # Temporarily swap dir for serialization if base_dir provided
    original_dir = state["dir"]
    if base_dir:
        state["dir"] = run_dir

    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        state["dir"] = original_dir


def load_state(run_dir: str, base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Load state from run_dir. If base_dir provided and stored dir is relative, resolve it."""
    path = os.path.join(run_dir, STATE_FILE)
    if not os.path.isfile(path):
        raise ArenaError(f"no {STATE_FILE} in {run_dir}")
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)

    # Resolve relative dir: against base_dir when provided, else against run_dir
    stored_dir = state.get("dir", "")
    if not stored_dir:
        state["dir"] = run_dir
    elif not os.path.isabs(stored_dir):
        root = base_dir if base_dir else run_dir
        state["dir"] = os.path.join(root, stored_dir)

    return state


def migrate(state: Dict[str, Any], base_dir: str) -> Dict[str, Any]:
    """Migrate state from older schema versions to current.

    v1 (upstream-like): has "version":1, absolute "dir", no "state_schema_version"
    v2 (current): adds "state_schema_version":2, relative "dir" under base_dir

    Returns the migrated state (modified in-place).
    """
    # Detect v1: has version but no state_schema_version
    if state.get("version") == 1 and "state_schema_version" not in state:
        # Upgrade to v2
        state["state_schema_version"] = CURRENT_STATE_SCHEMA_VERSION
        # Relativize dir against base_dir
        abs_dir = state.get("dir", "")
        if os.path.isabs(abs_dir):
            try:
                state["dir"] = os.path.relpath(abs_dir, base_dir)
            except ValueError:
                pass  # keep absolute if can't relativize (e.g., different drives)
    elif state.get("state_schema_version", 0) < CURRENT_STATE_SCHEMA_VERSION:
        # Future migrations would go here
        state["state_schema_version"] = CURRENT_STATE_SCHEMA_VERSION

    return state


def ensure_current_schema(state: Dict[str, Any], base_dir: str) -> Dict[str, Any]:
    """Load and migrate if needed. Convenience wrapper for callers."""
    return migrate(state, base_dir)


# ------------------------------------------------------------ Plan / Totals

def plan_rows(n: int, wave: int = DEFAULT_WAVE) -> List[Dict]:
    rows = []
    for rnd, alive in enumerate(bracket_sizes(n)[:-1], start=1):
        m = alive // 2
        waves = 2 * math.ceil(2 * m / wave) + math.ceil(m / wave)
        rows.append({
            "round": rnd, "alive": alive, "matches": m, "bye": alive % 2,
            "calls": CALLS_PER_MATCH * m, "waves": waves
        })
    return rows


def plan_totals(n: int, wave: int = DEFAULT_WAVE) -> Dict:
    rows = plan_rows(n, wave)
    calls = n + sum(r["calls"] for r in rows)
    waves = math.ceil(n / wave) + sum(r["waves"] for r in rows)
    return {"agents": n, "rounds": len(rows), "calls": calls, "waves": waves, "rows": rows}


# ------------------------------------------------------------ Exports

__all__ = [
    # Constants
    "DEFAULT_AGENTS", "QUICK_AGENTS", "DEFAULT_WAVE", "ROOT", "LATEST",
    "STATE_FILE", "PHASES", "CALLS_PER_MATCH", "WEIGHTS",
    "CURRENT_STATE_SCHEMA_VERSION",
    # Errors
    "ArenaError",
    # Strategies / Cards
    "load_strategies", "combo_count", "deal", "agent_ids", "bracket_sizes",
    # State
    "new_state", "current_round", "record", "advance", "rounds_played", "alive_ids",
    # Scoring
    "weighted_total", "extract_json", "decide", "collect",
    # Jobs
    "phase_jobs", "missing_jobs",
    # State I/O + Migration
    "save_state", "load_state", "migrate", "ensure_current_schema",
    # Plan
    "plan_rows", "plan_totals",
]
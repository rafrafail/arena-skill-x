---
name: trix-arena
description: >-
  Tournament arena for Trix Agent. Run /arena with a task and N independent
  LLM participants get the same task, each with a different strategy card,
  then attack, defend and judge each other in a single-elimination bracket
  until one solution survives. Use when the user is not satisfied with an
  answer, calls it bad, says try again or do better, says "arena", or asks
  to make answers compete.
argument-hint: "[ping | plan | start | status | result | cancel] <task>"
---

# trix-arena

Trix Arena replaces "one more retry" with a tournament: N independent
participants get the exact same task, each answers with a different
strategy card (reasoning mode, workflow, strategy), then they attack,
defend and judge each other in a single-elimination bracket until one
solution survives.

Participants are plain LLM calls (safe MVP): no shell, no files, no web
access. Everything a participant needs must be inside the task text.

## When to invoke the arena

Invoke the arena when:

- the user is not satisfied with an answer and a plain retry is unlikely
  to help ("this is bad", "try again", "do better", "still wrong");
- the user says "arena" or asks to make answers compete;
- one high-stakes answer justifies burning roughly a hundred LLM calls:
  an architecture decision, a tricky bug, an important piece of text.

Do NOT invoke the arena when:

- the user just wants a rewrite, a fact, or a quick edit;
- the question is trivial enough that one retry will do — even the quick
  preset costs 91 calls;
- the task requires tools, files or fresh web data inside the solutions
  (participants cannot use any of those).

## Choosing a mode

| mode | invocation | participants | cost (wave 10) |
|------|------------|--------------|----------------|
| quick | `/arena start "<task>" --agents 16` | 16 | 4 rounds, 91 calls, 16 waves |
| full (default) | `/arena start "<task>"` | 100 | 7 rounds, 595 calls, 70 waves |
| custom | `--agents N --wave W --seed S` | 1 up to the strategy-card cap | always preview with `/arena plan` first |

Rules of thumb:

- The default is 100 participants. Before anything bigger than the quick
  preset, run the preflight below and confirm the cost with the user,
  unless they explicitly asked for the arena.
- `--wave W` only changes parallelism: how many jobs are batched per
  wave. Total calls do not change with wave (see the preflight numbers).
- `--seed S` fixes the bracket: same seed + same participant count means
  the same pairings, so reruns stay comparable.
- Counts above the strategy-card cap are rejected with a clear error
  (`agents must be between 1 and <cap>`).
- A single participant (`--agents 1`) just answers the task: no matches,
  1 LLM call. Useful as a smoke test of the task text itself.

## The canonical task package

`start` takes ONE task string, and every participant, attacker, defender
and judge sees exactly that string — so it must be self-contained. A
well-formed task package has four parts:

1. **Objective** — what the final answer must deliver and in what form
   ("return working Python 3 in a single code block ...").
2. **Inputs** — paste all relevant data, code or context inline;
   participants cannot open files or browse the web.
3. **Success criteria** — what "better" means: correctness bar, edge
   cases to handle, style, length, language.
4. **Constraints** — forbidden approaches, must-use pieces, audience.

Example:

```
/arena start "Write a Python 3 function is_balanced(s) returning True if
the brackets ()[]{} in s are balanced. Handle nesting and ignore other
characters. Judged on correctness, edge cases (empty string, interleaved
brackets) and clarity. Return one code block plus a two-sentence
explanation." --agents 16 --seed brackets
```

Bad task: "make it better" — what is "it", and better how? If the user's
complaint refers to an earlier answer, embed that answer verbatim in the
task text and spell out what "better" means.

## Preflight: know the cost before you start

Before an expensive run, call `plan`. It is pure: nothing is created,
nothing is written.

```
/arena plan
/arena plan --agents 16 --wave 10
```

Output for the quick preset:

```
arena plan: 16 participants, 4 rounds, 91 LLM calls in 16 waves
round  alive  matches  bye  calls  waves
    1     16        8    0     40      5
    2      8        4    0     20      3
    3      4        2    0     10      3
    4      2        1    0      5      3
```

Headline numbers for the two presets (wave 10):

- 16 participants (quick): 4 rounds, 91 LLM calls, 16 waves
- 100 participants (default): 7 rounds, 595 LLM calls, 70 waves

The arithmetic behind any custom N, so you can sanity-check the preview:

- the bracket pads N up to a power of two; each round halves the field
  (odd counts get one bye);
- every participant spawns exactly one answer: N calls up front;
- every match costs exactly 5 calls: 2 attacks + 2 defenses + 1 judge;
- waves are batch counts at your wave size — change the wave and the
  batch counts change, the call total never does;
- if a champion is judged against a baseline answer, add exactly one
  more call (the final) — the plan preview does not include it.

The tool form returns the same numbers as data: `arena_plan` answers
`{"ok": true, "plan": {"agents", "rounds", "calls", "waves", "rows"}}`.

## Driving a run: the six subcommands

| command | flags | effect |
|---------|-------|--------|
| `/arena ping` | — | health check; answers exactly `pong` |
| `/arena plan` | `--agents N` `--wave N` | cost preview, touches nothing |
| `/arena start "<task>"` | `--agents N` `--seed S` `--wave N` `--dir PATH` | create a run, deal strategy cards, open round 1 |
| `/arena status` | `--run ID` `--dir PATH` | inspect: alive, rounds, champion, pending jobs |
| `/arena result` | `--run ID` `--dir PATH` | collect judge verdicts for the open round, report |
| `/arena cancel` | `--run ID` `--dir PATH` `--purge true\|false` | stop the run; purge also deletes its directory |

Defaults that just work: omit `--run` and every read targets the latest
run (the `LATEST` pointer written at start); omit `--dir` and runs live
under `.arena` in the working directory. Run ids look like
`brackets-20260101-120000` (slug of the seed + timestamp).

What each subcommand prints:

`/arena ping`:

```
pong
```

`/arena start "..." --agents 16`:

```
arena start: run brackets-20260101-120000 created under .arena/brackets-20260101-120000 (16 participants, seed 'brackets').
16 spawn jobs pending; follow with '/arena status' or the arena_status tool.
```

`/arena status`:

```
arena status: run brackets-20260101-120000 (dir .arena/brackets-20260101-120000)
task: Write a Python 3 function is_balanced(s) ...
alive 8/16, rounds played 1/4, current round 2, champion: none, cancelled: no
jobs (missing/total): spawn 0/16 missing, attack 0/8 missing, defend 0/8 missing, judge 2/4 missing
unrecorded matches: r2-m02
```

The `jobs (missing/total)` line is exactly the work still to be done;
`finished: yes` appears once a champion exists and an optional final has
been judged.

`/arena result` (no baseline in this run, so there is no final line):

```
arena result: run brackets-20260101-120000 - alive 8, rounds played 1, champion: none, finished: no
collected verdicts: 4
  r1-m01: recorded - a003 beat a001, 78.5 to 71
  r1-m02: recorded - a002 beat a004, 66 to 60.5
  r1-m03: recorded - a006 beat a005, 71 to 64
  r1-m04: recorded - a007 beat a008, 69.5 to 58
```

`/arena cancel`:

```
arena cancel: run brackets-20260101-120000 cancelled (purged: no)
```

Errors are one-liners and never guessed: unknown options, single-dash
typos, missing values and wrong types all fail loudly
(`arena plan: option --agents expects an integer, got 'x'`), a missing
task fails with usage
(`start needs a task: arena start "<task>" [--agents N] [--seed S] [--wave N] [--dir PATH]`),
and an unknown subcommand prints the known list.

Tool equivalents: `arena_ping`, `arena_plan`, `arena_start`,
`arena_status`, `arena_result`, `arena_cancel`. Every tool answers
`{"ok": true, ...}` on success or `{"ok": false, "error": "..."}` on
failure and never raises toward the host.

## Executing the jobs: who actually runs the tournament

`start`/`status`/`result`/`cancel` manage the run; the jobs themselves
are executed through the orchestrator
(`plugins/trix-arena/arena/orchestrator.py`, importable as
`trix_arena.arena.orchestrator`). Its contract in one paragraph:

- `orchestrator.run(state, backend)` walks the pending jobs phase by
  phase (spawn, then per-round attack, defend, judge, then the optional
  final), slicing them into wave batches of the run's wave size;
- `backend` is one callable that performs a single LLM call for a job's
  prepared prompt — its exact signature is documented in the orchestrator
  module docstring. A host-side runner tool with a built-in backend is
  not wired in this version (planned with backend integration), so today
  the host agent supplies this callable;
- only transport and schema failures are retried (at most 2 retries, so
  3 attempts); anything else fails the job immediately. Exhausted
  retries pause the run between jobs with the failure recorded;
- state is saved atomically after every successful job, and jobs carry
  idempotency keys: after a crash or a pause nothing is re-executed and
  there can never be two champions;
- `pause(state)` / `cancel(state)` take effect at job boundaries (the
  in-flight job finishes and is saved first); `resume(state)` re-opens a
  paused or crashed run; a cancelled run is terminal — `run()` on it
  raises.

From the command surface you just watch `/arena status` until
`finished: yes`, then read `/arena result`.

## The final answer

When `/arena status` says `finished: yes`, `/arena result` gives you
everything needed for the user-facing summary. Report these four things,
in this order:

1. **Winner** — the champion id and how it got there, from the result
   header (`champion: a003`) and the `collected` lines (e.g.
   `a003 beat a001, 78.5 to 71`).
2. **Score** — if the run has a baseline, quote the final line verbatim:
   `final: champion is better (champion 84.5 vs baseline 71) <judge reason>`.
   Totals are weighted rubric scores with weights correctness 30,
   completeness 25, specificity 15, robustness 20, clarity 10 (so on a
   0-100 scale). If there is no baseline there is no final — report the
   winning match score instead.
3. **Survived attacks** — each judged match records the attacks the
   winner survived (`survived` lists and judge reasons are kept per
   match in the run's `arena.json`; the `collected` lines give match ids
   and scores). Summarize in one sentence: what was attacked and what
   held.
4. **Limitations** — close with the honest caveats from the next
   section, plus anything the judge's `reason` flagged.

If `/arena result` says `final: pending`, the final verdict has not been
collected yet — let the orchestrator finish, then read the result again.

## Limits of the arena

- Participants are independent LLM calls with no shell, files or web
  access (safe MVP): the task text is their whole world.
- Judges are LLMs too — scores are heuristic, not ground truth.
- The bracket is single-elimination: one bad judging decision eliminates
  a good answer for good.
- A run is only as reproducible as its backend: the seed fixes pairings,
  not model sampling.
- Cancel is terminal, and `--purge true` deletes the run directory —
  there is no undo.

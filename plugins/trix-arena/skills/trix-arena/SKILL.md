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

Trix Arena runs a tournament instead of another retry: N participants get
the exact same task, each with a different strategy card (reasoning mode,
workflow, strategy), then they attack each other in a bracket until one
solution is left.

## Stage 0 status

This skill file is the skeleton of the built-in skill for the Trix Arena
plugin. What exists right now:

- The `/arena ping` command: always answers `pong`. Use it to check that
  the plugin loaded.
- The `arena_ping` tool: same health check, callable as a tool.

What is planned next (do not try to use yet):

- `plan` — estimate participants, rounds and LLM calls before running.
- `start` — start a tournament on a task.
- `status` — show current round, matches, survivors.
- `result` — winner, score, surviving attacks, limitations.
- `cancel` — stop a running tournament.

## Using the ping

```
/arena ping
```

If the plugin is loaded you get back exactly:

```
pong
```

Anything else means the plugin did not load (or a later stage changed the
command surface; check the plugin changelog).

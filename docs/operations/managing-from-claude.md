<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Managing the harness from an agent

The harness exists so that local models do the coding work. An agent that
supervises a run should therefore never read a file, write a patch or run a
test: it writes the plan, starts the run, and from then on reads decisions.
That is the whole economic case, and the surface is shaped to enforce it.

## The loop

```text
write SPEC.md and PLAN.md  →  plan_submit  →  plan_approve  →  implement_start
                                                                     │
                       merge by hand  ←  run_tasks  ←  run_status (poll)
```

1. **Write the plan.** This is where an agent's quality is worth paying for:
   decomposition into tasks that are each one logical change. The harness does
   not care who wrote it - entry point C of [the pipeline](../concepts/pipeline.md).
2. **`plan_submit(markdown)`** validates it against the `Plan` schema and
   stores it by digest. An invalid plan is refused with the reason, not
   repaired.
3. **`plan_approve(plan_digest)`** marks it as the one `nih implement` runs.
4. **`implement_start()`** returns a run id immediately; the run outlives the
   call.
5. **`run_status(run_id)`** and **`run_tasks(run_id)`** are how you follow it.
   `run_events(run_id, since=cursor)` gives one line per event when something
   needs explaining.
6. **Merging is a human action.** The run commits to `harness/run-<id>`; nobody
   merges it for you.

## Wiring it up

```bash
claude mcp add niharness -- nih mcp -C /path/to/project
```

The server speaks over stdio and manages one project. It is the same surface
`nih runs`, `nih status --json` and the TUI use, so nothing about a project
changes depending on who is driving it.

## What it costs

Every answer is a digest with a budget, built from the primitives the harness
already uses to keep tool results small, and it says when it truncated.

| Call | What comes back | Rough cost |
|---|---|---|
| `implement_start` | run id, where the tools ran, task count | ~120 tokens |
| `run_status` | stage, counts, commits, interventions, context fill | ~150 tokens |
| `run_events(since)` | counts by type, then one line each | ~200 tokens |
| `run_tasks` | per task: steps, commits, why it stopped | ~250 tokens |
| `run_diff` | the change under review | up to ~2000 tokens |

Estimates, not measurements. The point is the order of magnitude: supervising a
ten-task run should cost single-digit thousands of tokens, against the hundred
thousand that reading `events.jsonl` and the diffs directly would.

## What the surface refuses

<div class="refusal" markdown>
No `read_file`. No `apply_patch`. No `git_commit`. No way to run the gate.
There is a test asserting their absence.
</div>

Not as a safety railing against a malicious agent - as a design constraint
against a helpful one. An agent that can patch will patch, and then the
expensive model is doing the work the resident local one was there to do, at
roughly a hundred times the price. The gate also stops being the only way in.

`run_diff` is the one call that returns source, and it returns what the run
itself produced.

## Steering a run that is already going

| Want to | Call | What happens |
|---|---|---|
| stop it | `run_cancel(run_id, reason)` | a request lands in the run's inbox; the run stops at its next step boundary and leaves a resumable snapshot |
| answer a question | `questions_answer(run_id, answers_json)` | the answers reach the step that is waiting |
| approve a plan | `plan_approve(digest)` | the next `implement_start` uses it |
| continue a stopped run | `run_resume(run_id)` | the tasks it already finished are skipped; the rest run in the same worktree, appending to the same log |

These are requests, not events: the run writes the event when it acts on one.
See [ADR 0015](../adr/0015-control-actions-are-requests.md) for why, and
[Events](../concepts/events.md) for what that means for the log.

## When there is no agent

Everything above has a human equivalent, because the surface is one
implementation with three callers:

```bash
nih runs                      # the registry
nih status <run-id>           # the folded snapshot
nih implement --detach        # start a run in the background
nih cancel <run-id>           # the same inbox line an agent writes
nih resume <run-id>           # continue where it stopped
nih-tui --run .nih/runs/<id>  # follow it live; c asks it to stop
```

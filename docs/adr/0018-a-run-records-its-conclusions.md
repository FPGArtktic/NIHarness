<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0018. A run records its conclusions, so it can be continued

- Status: Accepted
- Date: 2026-10-22

## Context and problem statement

A cancelled run was resumable in principle - the event log is complete and the
worktree keeps its commits - but nothing could actually continue one, and the
reason was a gap in the log.

The log recorded every step a task took: which stages ran, which tools were
called, which commits landed. It did not record what the run *concluded* about
the task. Whether `t3` got through review was answerable only by the process
that ran it, because a reviewer's ACK lives in a model response, not in an
event. A continuation would therefore have had to re-run tasks that were
already done, or guess from the shape of the steps.

## Decision drivers

- Everything that matters about a run must be derivable from its log; that is
  the premise the whole design rests on.
- A resume must not redo work that is already committed on the branch.
- A refusal is better than a guess when the inputs are not there.

## Considered options

1. **Emit `task.finished` when a task reaches a terminal stage, and read it
   back when resuming.**
2. Infer completion from the steps: a review step followed by no fix step.
3. Keep a separate progress file next to the log.

## Decision outcome

Option 1. `task.finished` carries the task, the outcome (`completed` or
`handoff`), how many revisions it took, the commits it produced and why it
stopped. The fold turns that into per-task progress, so `run_tasks`, `nih
status` and a resume all read the same thing.

`nih resume <run-id>` then: refuses a run that is still live, or finished, or
that never recorded which plan it was executing; loads that plan from the store
by digest; opens the worktree the run already has - `Vcs.open_worktree`, added
to the port for this - and runs the tasks the log does not mark done, appending
to the same event log after a `run.resumed`.

Option 2 reconstructs a conclusion from circumstantial evidence, which is the
habit this project keeps refusing. Option 3 adds a second source of truth next
to a log that is already the first one.

## Consequences

- A supervisor's workflow becomes: cancel, fix the plan, resume - and the
  tasks already committed are not paid for twice.
- A run resumed after its plan was edited would skip tasks by id against a
  different plan. The resume loads the plan by the digest the run recorded, so
  an edited plan is a different digest and the resume refuses rather than
  running a hybrid.
- `run_tasks` now answers from the log alone, which is what let the management
  surface report per-task outcomes without a second bookkeeping path.
- A run whose worktree was removed cannot be resumed; the adapter says so
  instead of creating a new one and stranding the commits.

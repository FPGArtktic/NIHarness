<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0014. A step declares which artifact ends it, and review starts from the diff

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

Wiring the review and fix stages ran into three things the earlier design had
not needed:

1. The loop ended a step on a `FinalAnswer`. The reviewer's artifact is a
   `Verdict`, and a reviewer that answered with a filled Definition-of-Done
   checklist would be accepted as finished.
2. The reviewer needs to know what changed. It re-runs the build and the tests
   itself - that is the point of it - but reconstructing the diff from the task
   description and a `git status` is work it should not have to do.
3. The quick gate runs inside the tool server, because that is where
   `git_commit` lives. So the harness never *runs* the commit gate stage the
   pipeline describes.

## Decision drivers

- A step's terminal artifact is a property of the role, and the loop should
  enforce it rather than hope.
- The harness must not claim to have run something it did not run.
- The pipeline transitions are already written and tested; the wiring should use
  them, not re-implement a simpler flow beside them.

## Considered options

For the gate stage specifically:

1. **Observe it: a commit landed, or it did not.**
2. Re-run the quick gate from the harness after each step.
3. Trust the tool result's `ok` flag.

## Decision outcome

- `LoopConfig.final_artifact` names the artifact that ends a step
  (`FinalAnswer`, `Verdict`, or none for a prose step), the loop validates
  against it, and `LoopState` exposes `final_answer` and `verdict` so a caller
  cannot read one as the other.
- `Vcs.diff` was added to the port, and the reviewer's and fixer's windows carry
  the commit under review. The reviewer still re-runs build and tests; the diff
  is a starting point, not evidence.
- The commit gate stage is **observed**, option 1: the run records HEAD before
  the step and asks afterwards whether it moved. A commit exists if and only if
  the gate passed, because the tool server commits only on green. When one
  landed, the harness emits `vcs.committed` derived from the diff, so the event
  log records the commit even though the harness did not make it.

Option 2 would run the gate twice for every step, doubling the slowest part of a
run to learn something the worktree already states. Option 3 trusts a flag over
the repository, which is the habit this project exists to break.

## Consequences

- A task now runs implement → commit gate → review, with fixes bounded by
  `max_revisions`, using the domain's own transitions.
- `ImplementationRun` requires version control and a worktree. A run without one
  cannot tell whether a commit landed, and pretending otherwise would make the
  commit gate meaningless.
- The full gate tier still does not run: a task that the reviewer acknowledges
  ends with `full_gate_pending`, and the command says so rather than implying
  the full tier passed.
- A model that answers a review step with the wrong artifact is asked again,
  bounded by the same three attempts as any malformed answer.

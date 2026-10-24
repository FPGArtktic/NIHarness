<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0028. A whole task is graded by tests it never saw

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

L0 measures throughput, L1 whether a model can call tools, L2 whether it can
review. None of them answers the question somebody using this harness
actually has: **give it a task, does the work it commits do what was asked.**
That is L3, and §11 asks it for `pass@1`, wall time, tokens, tool calls and
harness overhead outside inference.

Three things had to be decided, and each is a way the layer could have
measured something other than what it claims.

**What grades a task.** The pipeline already runs tests - the gate runs them
before every commit, and the reviewer re-runs them itself. So the cheap
answer is to score a task green when its own gate went green. That scores
nothing. A model that writes `assert isinstance(result, float)` passes its own
gate, and L2 has a case about exactly that mistake being waved through. A
model's own tests are evidence it gathered, which is what the gate is for and
what a benchmark may not use as the grade.

**Whether to vendor a reference set.** §11 names a SWE-bench subset. It is
not available here: the harness is air-gapped, and vendoring it is a
download, a license file and a `THIRD_PARTY.md` entry that nothing in this
session could do. Waiting for it would leave the layer the README calls the
first blocker unbuilt for the sake of a task set.

The suite file that existed was a **placeholder**, like L2's was: two tasks
naming `fixtures/py-cli`, `fixtures/c-ring`, spec files and hidden-test files
that were never written, and a `[sources]` section pointing at a
`vendor/swebench-lite-subset` that is not there. It declared four languages
and described a format rather than running anything.

**How the task's brief reaches the model.** A task graded on criteria it was
not told is measuring telepathy. `Task` had nowhere to put them: a plan's
task carries `satisfies`, which points at a spec's acceptance criteria *by
id*, and an id is a reference a model cannot read.

## Decision

**A task is graded by tests kept out of the worktree and applied after the run
ends.** `benchmarks/suites/l3/hidden/<task-id>/test_*.py` is copied into the
finished tree once the run has stopped and its commits are made, and the
result of running them is the task's grade. `pass_at_1_ratio` counts tasks
that both committed something and passed those tests; `committed_ratio` is
the ceiling on it, because work that did not land did not happen.

**The suite's own fixture, written here.** A small ledger in whole cents, with
three tasks. Not a stand-in for SWE-bench, which stays on the roadmap: a
locally written suite is a suite whose fairness can be checked, which matters
more for the first three tasks than scale does.

**`Task.acceptance` carries the brief.** A tuple of sentences, rendered by
`render_task` as its own section. The criteria are given to the model
verbatim, and the hidden tests check exactly those sentences and nothing
else. A hidden test that demands a choice the brief did not state is a bug in
the case.

## Consequences

The layer runs the **real** pipeline: `ImplementationRun`, the same object
`nih implement` drives, so implement, the commit gate, review and the bounded
fix loop are measured as they run rather than as a rig that resembles them.
Two metrics §11 asked of L2 fall out of it for free, because they belong to a
whole task rather than to a step: `first_attempt_gate_ratio` and
`revisions_per_task`.

A fair case is now a thing that can be got wrong in a specific way - a hidden
test checking something the `acceptance` lines do not say - which is a better
failure mode than a grade nobody can audit.

Three tasks is not ten per language, and they are all Python: the
placeholder's C, Rust and Bash fixtures were never written and are not
written now. The shortfall is on the roadmap; a layer that runs on three
tasks can at least say whether it runs.

The format changed with it. The placeholder gave each task its own `repo`,
`spec` file and hidden-test file; this suite has one fixture and
`hidden/<task-id>/`, which is what three tasks on one language need and is
simpler to check. A second language will need a fixture per task, and that is
a change to make when there is a second language rather than a field to carry
until then. The declared metric names also changed - `pass_at_1_ratio` rather
than `pass_at_1` - so they read like L1's and L2's; everything the
placeholder promised is still recorded, with the clock split into
`inference_seconds`, `tool_seconds` and `overhead_seconds`.

`Task.acceptance` defaults to empty, so no existing plan changes. The planner
does not fill it yet: doing so is a prompt change, which needs a golden
update and a measurement, and this record does not make one.

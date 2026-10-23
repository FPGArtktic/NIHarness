<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0024. A benchmark layer is scored by folding the event log

- Status: Accepted
- Date: 2026-10-23

## Context and problem statement

The benchmark methodology has existed since the project began, with four
layers and named metrics, and no runner. Every claim about a model was
therefore an impression from reading transcripts - which is what the
methodology itself calls not a measurement.

Two things had to be decided to build L1. Where the tasks act, and how a run
becomes a number.

## Decision drivers

- A number has to be comparable between two harness commits, or a regression
  cannot be bisected.
- Scoring must not need a judgement. A benchmark that needs somebody to read a
  transcript cannot be re-run for a number six months later.
- The conditions must be the real ones. A number from a rig that merely
  resembles the harness measures the rig.
- The grader itself has to be testable, or a number says as much about the
  grader as about the model.

## Considered options

For where tasks act:

1. **A fixture that ships with the suite, copied per task.**
2. This repository's own tree.
3. A vendored external repository.

For scoring:

1. **Fold the run's events.**
2. Inspect the worktree afterwards.
3. Have a model judge the transcript.

## Decision outcome

**A fixture, and a fold.**

Each task gets a fresh copy of `benchmarks/suites/<layer>/fixture/`, made into
a git repository of its own. A repository rather than a directory because the
tools require one: the gate commits, `apply_patch` reverses a diff to tell
"already applied" from "does not apply", and the state-cycle detector hashes
the tree. The harness's own tree was rejected outright - a target that changes
with every commit makes two records incomparable, which is the one thing a
benchmark may not do.

The task then runs through the **same loop that runs real work**: the same
validation, the same allowlist, the same detectors, the same budgets, and the
same `FinalAnswer` artifact demanded at the end. Scoring reads the payloads
that run appended - `tool.called` for the sequence and the arguments,
`tool.rejected` for what the harness refused and under which rule,
`loop.intervention` for incidents, `step.finished.stop_reason` for whether the
model ended the loop or the harness did.

So the grader is a pure function over recorded events, in
`niharness.domain.bench`, unit-tested on synthetic sequences with no model
present. Option 2 was rejected because the worktree says what a model achieved
and nothing about how: a correct file after sixty redundant calls scores the
same as one after eight, and `loop_incidents_per_task` exists precisely to
tell those apart. Option 3 was rejected for the reason the methodology gives:
a judged benchmark is not re-runnable.

### Consequences

- Good: L1 exists, and the first run against a 4B produced the project's first
  measured numbers for tool calling.
- Good: the first run found **three expectations that measured something other
  than what they claimed** - a task pinning `grep` when listing the tree and
  reading the file answers just as well; a refusal task failing a model for a
  harmless call made after the refusal; and a task asking for `shell`, which
  is never offered to the role and therefore cannot be called, so the rule it
  expected could not fire. Those are suite bugs that only a run can find, and
  finding them is most of what a first run is for.
- Good: because the suite and the real pipeline share a loop, a bug in one is
  a bug in both. `LoopConfig` let a caller demand an artifact without carrying
  its schema, which meant the schema was never sent and no answer could ever
  validate; eight benchmark tasks ran to `StopReason.ERROR` before that was
  obvious. The config now derives the schema from the artifact.
- Bad: a suite corrected after seeing a model fail it is no longer an
  independent test of that model. The records say which harness commit they
  were taken at, and a reading has to account for it.
- Neutral: the metrics are only as good as the task set. Eight of the twenty
  the methodology asks for exist, and a model passing all eight says the tasks
  are easy as much as it says anything about the model. What the first runs
  showed that *is* informative is the refusal tally: of the calls the harness
  refused, almost all were the commit message's form rather than anything
  about the code.

## Follow-ups

- The remaining twelve L1 tasks, and harder ones - a model passing everything
  measures nothing.
- L2 needs a golden set with human verdicts to score reviewer precision
  against; L3 needs the vendored reference sets. Both are on the roadmap.

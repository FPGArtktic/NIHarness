<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0005. `git_commit` is the quality gate

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

A model will report success it did not verify. Not maliciously: it predicts the
shape of a successful ending. Any design that trusts "tests pass" as a claim
will accumulate commits that do not build.

## Decision drivers

- Red must mean no commit, with no path around it for either agents or humans.
- The check must be cheap enough to run on every commit attempt.
- A repeated failure must be machine-comparable, so looping is detectable.

## Considered options

1. **Make the commit tool the gate: format, lint, incremental build, quick
   tests, then commit.**
2. Commit freely, gate in CI.
3. Gate once per task, after the model says it is finished.

## Decision outcome

Option 1, in two tiers. The quick tier runs on every commit attempt; the full
tier (complete build, integration tests, sanitizers, bitbake, ptest,
documentation link check) runs before a merge. Yocto never runs in the quick
tier.

The commit message is validated as Conventional Commits with a body, because
"explains why" is a reviewable property. Gate output is normalised - paths, line
numbers, durations and addresses replaced - and digested into a **failure
signature**, which is what the gate-thrash detector counts.

On red, the worktree is rolled back to the last green commit and the step
restarts with a summary of what failed.

## Consequences

- There is no `--no-verify`. An agent that cannot pass the gate reports a
  blockage, which is the useful outcome.
- The reviewer re-runs the build and tests itself rather than reading the
  implementer's claims.
- Every commit attempt costs a lint and test cycle; incremental builds and
  `ccache` make that acceptable, and it is the cost of the guarantee.
- A toolchain missing from the sandbox is red, not skipped. A silently skipped
  check is worse than a failing one.

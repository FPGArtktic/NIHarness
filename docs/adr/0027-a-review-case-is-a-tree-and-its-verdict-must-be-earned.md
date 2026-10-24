<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0027. A review case is a tree, and its verdict must be earned

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

L2 measures review: given a change and the task it claims to do, does the
reviewer reach the verdict a human reached. Three things had to be decided
before a single case could be written, and each of them is a way the layer
could have measured something other than what it claims.

**How a case is stored.** The obvious answer is a `.patch` file, which is what
the placeholder suite declared - three cases pointing at patches that were
never written.

**What counts as a correct NAK.** The obvious answer is "it said NAK", which a
reviewer that refuses everything passes with full marks.

**Whether to check the reviewer's evidence.** `Verdict` already refuses an ACK
with `tests_passed = false`, so the model must *claim* green. Nothing checked
whether the claim was earned.

## Decision drivers

- A golden set whose cases stop applying is worse than no golden set, because
  it fails quietly and the numbers keep coming.
- A metric that a degenerate strategy maximises is not a metric.
- The reviewer exists because an implementer's claim is not evidence
  ([ADR 0014](0014-review-starts-from-the-diff.md), §4). Whatever standard
  that imposes on the implementer applies to the reviewer.
- Everything the layer scores should be mechanical, so a number can be
  re-produced in six months ([ADR 0024](0024-a-benchmark-layer-is-scored-by-folding-the-event-log.md)).

## Considered options

For storage: a stored patch; a pair of trees with the diff generated; a
scripted mutation of the fixture. For a correct NAK: the decision alone; the
decision plus a file; the decision plus a file plus a required phrase; the
decision plus a file plus any of several tokens. For evidence: trust the
verdict; require a `run_tests` call; require a *green* `run_tests` call in the
same step.

## Decision outcome

**A case is a tree.** `cases/<id>/` holds replacement files; the runner copies
the fixture, commits it, copies the case over it, commits that, and the
reviewer reads what `git show` produces. A case cannot fail to apply, and a
case can be *read* - "what is wrong with this change" is answered by opening
the file rather than by reconstructing it from hunks.

**Every case is green.** Format, lint and tests pass after each one. This is
forced by where review sits: the gate runs first, so a defect the tests catch
never reaches a reviewer. Every NAK here is therefore a defect no test can
catch, which is the only kind a reviewer is for - and it is why the L1 fixture,
with its one deliberate failure, could not be reused.

**A NAK is scored on where and why.** A NAK case declares `defect_files` (a
note must be anchored to one of them) and `mentions_any` (one of several tokens
must appear in the reviewer's own words). Any rather than all, because a review
is free text and demanding a phrasing measures vocabulary rather than review.
The `L2Case` model refuses a NAK case that declares neither, so the suite
cannot grow a case without an answer key.

**The reviewer's claim must be earned.** A verdict saying the tests passed has
to point at a `run_tests` call in its own step that returned green.
`evidence_ratio` reports how often that held, and a case fails without it
however right the decision was. This is the most interesting single number in
the layer: a model that reads a diff and says "looks fine, tests pass" has
failed at precisely the thing a reviewer is for.

**NAK is the positive class**, stated rather than inferred: precision is how
many refusals were earned, recall how many real defects were caught. A case
that produced no verdict at all is counted as neither, because folding an
absent reviewer into recall makes it look cautious.

## Consequences

- The suite is eight cases, four of each verdict. Enough for a confusion
  matrix, not enough for a confidence interval, and the record says how many.
- `prepare_fixture` became suite-agnostic, and the across-passes report became
  generic over "unit" so both layers share it.
- The implementer half of L2 that §11 asks for - first-attempt gate pass rate,
  revisions per task - is not here. It needs implement, gate, review and fix
  run end to end, which is L3's shape; the roadmap says so.
- Adding a case means writing files and one `[[case]]` block, with no patch to
  maintain. The suite's own invariant (every case green) is checked by running
  the gate over each case, which is the next thing to automate.

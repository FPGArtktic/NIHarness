<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0025. Creation is an edit with nothing to find

- Status: Accepted
- Date: 2026-10-23

Extends [ADR 0022](0022-an-edit-located-by-its-own-text.md), which it does not
reverse: that record's reasoning is what makes this one possible.

## Context and problem statement

ADR 0022 gave the model `replace_in_file` and left **creating** a file to
`apply_patch`, on the grounds that a whole-file write "loses the one property
the diff requirement exists for: there is no bound on what a single call may
change". It also said two write tools were a cost, and that a model choosing
between them inconsistently was the price.

The first 14-task L1 run priced it. A 4B passed every writing task except one,
and the one it failed was creating a file - where it was pushed into writing a
creating diff by hand, which is the format the earlier runs had already shown
it worst at. `effective_ratio`, added in the same change, is what caught it: the
model called `apply_patch` and the call did not succeed, which a sequence
expectation alone would have scored as a pass.

So the harness was forcing the model into its weakest format for the one
change where it had no alternative.

## Decision drivers

- The measurement, rather than an opinion about what ought to be hard.
- ADR 0022's objection has to be answered, not ignored.
- Reviewability is unchanged: whatever reaches the reviewer and the commit is
  still a diff.
- The schema cannot express "empty only when the path is absent", because
  whether it is absent is I/O.

## Considered options

1. **An empty `find` creates the file, with `replace` as its content.**
2. Leave creation to `apply_patch` and improve its refusals further.
3. A separate `create_file` tool.
4. Let `replace_in_file` overwrite an existing file when `find` is empty.

## Decision outcome

**Option 1.** `replace_in_file(path, find="", replace=<content>)` creates the
file; a missing parent directory is created with it, inside the worktree.

ADR 0022's objection does not cover this case, and saying why matters more
than the change itself. What the objection protects is the bound on how much
one call may change, so that a review of that call means something. For a file
that **does not exist yet** there is nothing being replaced: the content *is*
the change, the diff the harness computes is a pure addition - the same diff
`apply_patch --- /dev/null` would produce - and the size cap bounds it exactly
as it bounds a diff. The objection applies to overwriting, which is why
**option 4 is refused**: an empty `find` against a file that is there does not
say what to change, and the tool server says so.

Option 3 was rejected because the cost ADR 0022 named is the *number* of write
tools, and a third would be worse than the second. One tool that creates when
there is nothing to find reads as one idea, not two.

### Consequences

- Good: creation goes through the tool that needs no hunk arithmetic. The task
  that failed passes, and the mechanism - not the ratio - is what says so: a
  contract test asserts the file lands, with its parent directory, inside the
  worktree.
- Good: `apply_patch` is now for a change across several places at once, which
  is a sentence a prompt can carry. It is still the only tool that can do that.
- Bad: the suite's creation task changed its expected tool between two runs,
  so those two records are not comparable on that task. The record carries its
  harness commit, which is the mechanism for exactly this, and the suite says
  so in a comment beside the task.
- Neutral: the schema accepts an empty `find` for any path, so the refusal for
  an existing file lives in the tool server. That is where path existence is
  knowable, and the domain still refuses the call's size and confinement.

## What the same runs said about measurement

Two passes of the identical 14 tasks, with nothing changed between them,
differed by three tasks: two that passed in one and failed in the other, and
one that failed in both. Reading either pass as a result would have been wrong
in both directions - it would have claimed a regression from a harness change
that provably fixed the task it targeted.

So `nih bench --repeat N` runs the suite N times and writes a record per pass,
and the human output reports each metric's range and names the tasks that were
not stable. A single pass of a stochastic model is an anecdote with a number
attached, and the runner should not make one easy to mistake for a measurement.

The one failure that repeated is worth naming: asked to change a value in two
files, the model changed one and committed. That is caught by `expect_content`,
an expectation about the **end state** rather than the route, added because
scoring the route would have failed a model for choosing `apply_patch` over two
edits when either is correct.

<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0022. An edit is located by its own text, and the harness writes the diff

- Status: Accepted
- Date: 2026-10-23

## Context and problem statement

Until now the only way a model could change a file was `apply_patch`, which
takes a unified diff. [ADR 0005](0005-commit-is-the-gate.md) and the project's
rules put it that way for a reason worth keeping: every change is reviewable,
and there is no `write_file` that can replace a file wholesale.

A unified diff is also a format the model has to *account* for. Hunk headers
carry line numbers and counts; context lines have to match the file byte for
byte; prefixes distinguish added from unchanged. None of that is the change -
it is bookkeeping about the change.

Live runs made the cost concrete. One 4B, asked for a register's supply
voltage, called `rag_query`, read the value out of an ingested datasheet whose
erratum contradicted the obvious answer, and wrote the correct constant with a
comment naming the revision. It then failed, three times in a row, to express
that one-line change as a diff: once with `read_file`'s line numbers in the
context, once with every line marked as context so the diff did nothing, once
with no headers at all. Finding the value was never the problem.

Each of those three has since been named or repaired, and each repair is a
patch over the same mismatch: the model knows the content and is being asked
for an encoding of it.

## Decision drivers

- The model is weak at bookkeeping and adequate at the change. Spend its
  budget on the second.
- Reviewability is not negotiable. Whatever reaches the reviewer, the gate and
  the history has to be a diff.
- A refusal must be actionable. "corrupt patch at line 9" is not; neither is
  "not found".
- No silent wrong edits. A tool that guesses which of two places was meant is
  worse than one that refuses.
- One more write tool is one more thing for a small model to choose between.

## Considered options

1. **A tool that takes the passage to replace and its replacement, and lets
   the harness compute the diff.**
2. Keep `apply_patch` alone and go on repairing diffs.
3. A `write_file` taking the file's whole new content.
4. Edits addressed through the language server by symbol name.
5. A GBNF grammar constraining the diff argument's shape.

## Decision outcome

**Option 1**, as `replace_in_file(path, find, replace)`.

`find` is text copied out of the file and must occur **exactly once**; that is
what makes the edit unambiguous without a line number, for the same reason the
symbol tools take a name ([ADR 0020](0020-language-servers-in-the-sandbox.md)).
An empty `replace` deletes the passage. The harness locates it, writes the
file, tells the language servers, and **shows the diff it produced** in the
result.

The rule in §4 relaxes from "unified diff only" to something more precise:
**every change is a diff by the time anything else sees it.** That was always
the property worth having. It is a statement about the output, and nothing
about it requires the model to have typed the input - a diff the harness
computed is well-formed by construction, which the hand-written one was not.

`apply_patch` stays, for creating a file and for a change spread over several
places at once. When it refuses a diff against a file that *exists*, the
refusal now names `replace_in_file` and how to call it: across live runs the
same model sometimes found the other tool after a refusal and sometimes
re-sent the diff until the detectors stopped the step, and the refusal is what
it is reading at that moment.

Option 3 was rejected because a whole-file write loses the one property the
diff requirement exists for: there is no bound on what a single call may
change, and a model that rewrites a file to add a line has made a review of
that call useless. Option 4 is good and narrower - it only reaches languages
with a server in the image, and only changes that are operations on a symbol -
so it stays a candidate rather than a replacement. Option 5 prevents malformed
*syntax* and nothing else; the context-only diff that started this was
syntactically perfect.

### Consequences

- Good: the class of failure disappears rather than being diagnosed. There are
  no hunk headers to count, so they cannot be counted wrong.
- Good: the refusals are about the *passage*, and each has an answer. Absent,
  with a near miss that differs only in whitespace, quotes the exact bytes
  back. Absent with no near miss names the closest line in the file. Present
  twice says how many and what would disambiguate it. Identical to the
  replacement says the edit would change nothing.
- Good: the result carries the diff, which is also the cheapest answer to
  "what does the file look like now" - otherwise a `read_file` the model pays
  for.
- Bad: two write tools where there was one, and a model has to choose. The
  prompt states the rule, the tool descriptions state it again, and a refused
  `apply_patch` states it a third time - but the choice is still the model's,
  and live runs show it is made inconsistently.
- Bad: `find` has to be copied exactly, and `read_file` prints line numbers
  that are not in the file. The near-miss message names that specifically,
  because it is the mistake the numbering invites.
- Neutral: the uniqueness rule means a change in five places is five calls.
  There is deliberately no `replace_all`: each call can be refused on its own,
  and a model that cannot locate one passage should not be rewriting five.

## Follow-ups

- Whether the two write tools should be one, and which, is an L1 question
  (`docs/benchmarks/methodology.md`): the metric is already defined as valid
  calls, correct arguments and loop incidents per task. Until there is a
  runner, the evidence is a handful of runs and is reported as such.

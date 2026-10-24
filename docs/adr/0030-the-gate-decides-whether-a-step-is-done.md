<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0030. The gate decides whether a step is done, not the model's checklist

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

An implement or fix step ended when the model produced a `FinalAnswer`, and
counted as **finished** only if that answer's Definition of Done was complete -
every one of ten items ticked. An unfinished step routed straight to
`Stage.HANDOFF`, before the commit gate ran at all.

So a model's self-assessment decided whether its work ever reached the gate.
§1 says the opposite: the gate is the only way in, and a claim is not evidence.
Every other claim in this harness is checked against the log - a reviewer's
"the tests passed" against a green `run_tests` call, an implementer's "it is
committed" against a green `git_commit` call - and this one was taken on trust
in the direction that discards work.

L3 measured what that costs.

**`handback_ratio` was 1.0 in every pass ever recorded**, including the pass
that committed two whole tasks whose work the hidden tests accepted. Those two
were committed, reviewed, refused, and sent to a fix step that reported an
incomplete checklist - so the pipeline handed back work that was right.

And the checklist is not weighing anything. Across fourteen passes, all ten
items appear in the unticked list about equally often (3 to 4 each): "no
dependency was added" as often as "`run_tests` succeeded", on tasks where no
dependency could have been added. That is a model returning the whole list
false, not one item at a time. The same field also arrived carrying the
reflection turn's prose, before that collision was fixed.

## Decision

**`StepResult.finished` means the step produced an answer**, not that the
answer ticked its own list:

```python
return self.stop_reason is StopReason.FINAL_ANSWER and self.answer is not None
```

A step that ran out of budget with nothing written is still unfinished - that
is a fact about the step, not a claim about it. Everything beyond that is
decided where it was always supposed to be: the **commit gate** sees what
landed, the reviewer reads the diff, the hidden tests grade the result.

The checklist is still asked for, still logged, and still the first thing a
human reads on a handoff. It is no longer the gate on reaching the gate.

**The commit check stops exempting an incomplete checklist.** It was written
on the reading that a model leaving items unticked is being honest about not
finishing; that reading is what the fourteen passes refute. So a step that
owes a commit and has none is asked once whatever its checklist says, with
tools still on and budget left to act.

## Consequences

An implement step that does nothing now reaches the commit gate, which finds
nothing landed and routes to the fix loop rather than straight to a human.
That is one more round of model time on a task that may be hopeless, bounded
by `max_revisions`, and it is the price of not discarding work on a
self-report.

The Definition of Done becomes what it reads like: a prompt that makes a model
check itself, and a record of what it thought. Its items are in the L3 record
as `unfinished`, which is how this decision was measured and how a later
change to the checklist would be.

Nothing about the gate moves. Nothing red is committed, and no step can commit
without `git_commit` going green - the only change is that a model can no
longer talk its work out of being offered.

Measured by L3, where `handback_ratio` 1.0 across every recorded pass is the
before.

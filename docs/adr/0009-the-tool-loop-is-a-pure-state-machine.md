<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0009. The tool loop decides, the executor acts

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

The tool loop is where the harness earns its keep: it validates every proposed
call, enforces budgets, runs the pathology detectors, climbs the intervention
ladder, and decides when a step is over. It is also the part most entangled
with the outside world - a model over HTTP, tools in a container, an append-only
log on disk.

Written the obvious way, as an async function that awaits all three, the
interesting behaviour becomes reachable only through fakes of everything, and
"what does the loop do on the third identical call" turns into an integration
test.

## Decision drivers

- Every rule in [ADR 0002](0002-hexagonal-architecture.md): the domain performs
  no I/O and reads no clock.
- The intervention ladder and the stop conditions must be testable directly, on
  synthetic inputs, because they are the behaviour that keeps a looping model
  from burning a whole budget.
- A run must replay to the same decisions, which rules out anything that
  depends on timing.
- The events a step emits should not have to be reconstructed by the caller.

## Considered options

1. **A pure state machine: `next_action(state)` decides, `observe_*(state, …)`
   folds the outcome and returns the events to append.**
2. An async loop in the domain that takes the ports as arguments.
3. The loop in the application layer, with only the detectors kept pure.

## Decision outcome

Option 1.

```text
action = next_action(state)        # CallModel | ExecuteTool | InjectResult | Rollback | Stop
                                   # the executor performs it
step   = observe_model(state, response)   -> Step(state, events)
step   = observe_tool(state, result, …)   -> Step(state, events)
```

Each transition returns the events for what happened, so the executor appends
what it is handed rather than inferring it, and the event stream cannot drift
from the state that produced it.

Two decisions inside the loop are worth recording, because both were bugs
first:

- **The repeat key is the digest of the *validated* arguments**, not of the
  proposal. Two calls that differ only in which defaults they spell out are the
  same call, and the repeat detector has to see them that way.
- **The ladder outranks the synthetic answer.** Answering a repeated call from
  the harness is rung one; once the ladder has climbed past it, the rung itself
  is the intervention. Otherwise a step could keep repeating while the ladder
  quietly escalated underneath it.

Option 2 was rejected because it breaks the purity rule for no gain. Option 3
splits the behaviour in two, which is how the loop's rules end up stated twice
and tested once.

## Consequences

- The whole ladder, the budget stops, forced finalisation and malformed-answer
  handling are unit tests over frozen inputs; the test module reads as the
  specification of the loop.
- The executor is deliberately dull: perform the action, feed back what
  happened, append the events. It is the only part that needs the ports, and it
  is where retries and timeouts live.
- One tool call is taken per turn. Fanning out would lose the ordering the
  detectors reason about, and a step that cannot see a result before choosing
  its next move is not a ReAct loop.
- The loop carries its own memory of results by argument digest, so a repeated
  call can be answered without touching the sandbox. That memory is a view, not
  a cache with a lifetime: it is cleared when a rollback restarts the step.

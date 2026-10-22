<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0015. Control actions are requests, delivered through an inbox

- Status: Accepted
- Date: 2026-10-22

## Context and problem statement

A run needed to be steerable from outside the process: stopped, told that a
plan is approved, given answers to the questions a step is waiting on. Three
drivers want to do that - a person at the TUI, an agent over MCP, a script at
the CLI - and the obvious implementation is for each to append the
corresponding event to `events.jsonl`.

That does not work, and the reason is concrete: `JsonlEventSink` assigns
sequence numbers in memory after scanning the file once. Two processes
appending would hand out the same `seq`, and the fold that derives run state
rejects a stream whose sequence does not increase. The event log has exactly
one writer by construction.

## Decision drivers

- One mechanism for every driver; a cancel from the TUI and a cancel from an
  agent must not be two code paths with two sets of bugs.
- The event log stays the single, ordered, replayable record.
- A stop must land between actions, never mid-tool, and must leave a resumable
  snapshot.

## Considered options

1. **An inbox: `control.jsonl` beside the log, many writers, drained by the run,
   which then writes the events itself.**
2. File-locked sequence allocation, so several processes may append events.
3. A socket the run listens on.

## Decision outcome

Option 1. A command is a *request*; the event is what the run writes when it
acts on one. `run.cancelled`, `plan.approved` and `questions.answered` already
existed in the catalogue, so nothing new had to be invented for the record
side.

Mechanics worth stating:

- Each submission is one `write` to a file opened with `O_APPEND`, which the
  kernel keeps whole, so concurrent drivers produce intact lines in arrival
  order.
- The consumer keeps a cursor file; the run is the only consumer, so the cursor
  has the single writer the log relies on.
- A line that will not parse is logged and skipped rather than stalling the run.
  The file keeps it for a human.
- The message id may be left empty by the submitter and is assigned by the
  inbox. That is what lets the TUI write a control line with nothing but a JSON
  serialiser - no digest implementation, and therefore no second dependency in
  the Rust crate.

Option 2 makes every writer carry locking correctness for a problem only
control has. Option 3 needs a listener, which means a run that is between steps
or paging in a model is not listening.

## Consequences

- Control is asynchronous by design: a request is honoured at the next step
  boundary. That is also what makes it safe.
- The log answers "what happened", the inbox answers "what was asked". A
  request that was never acted on is visible as an inbox line with no matching
  event - which is a useful thing to be able to see.
- Everything that steers a run now goes through one file whose format is in
  `schemas/ControlMessage.json`.

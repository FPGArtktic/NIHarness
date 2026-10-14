<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0003. Run state is an append-only event log

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

A run is long, interruptible, and interesting in retrospect: what did the model
try, what did the gate say, when did compaction fire, why was the task handed
back? A mutable state object answers none of those after the fact, and a TUI
that reads mutable state has to be told when to re-read it.

## Decision drivers

- `nih status`, `nih resume` and the TUI must agree without sharing code.
- A crash must not lose an event the harness already acted on.
- Post-hoc analysis (benchmarks, loop incidents, reviewer precision) needs the
  history, not a summary.

## Considered options

1. **Append-only `events.jsonl` per run, plus periodic snapshots, with state
   derived by a pure fold.**
2. A mutable run object persisted as JSON after each step.
3. SQLite with a normalised schema.

## Decision outcome

Option 1. One JSONL file per run; the sink assigns `seq` and `ts` and `fsync`s
before returning. State is `fold(events)`. Snapshots exist only so a resume need
not replay from zero, and are never a second source of truth.

Payloads are a discriminated union on `type`, exported to `schemas/` and
rendered into the generated event catalogue.

## Consequences

- Nothing mutates run state in place, anywhere, including compaction.
- The TUI computes nothing: metrics, ETA and compaction decisions arrive as
  events.
- Adding an event type is additive and cannot change an existing one.
- Logs grow; a long run means a long file, and the fold costs a pass over it.
  Snapshots keep that bounded in practice.
- `fsync` per event costs latency. An event the harness acted on but lost would
  cost more.

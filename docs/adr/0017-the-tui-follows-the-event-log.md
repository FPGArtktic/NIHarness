<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0017. The TUI follows the event log

- Status: Accepted
- Date: 2026-10-22
- Supersedes: the Unix-socket feed described in CLAUDE.md §10 and §15

## Context and problem statement

The TUI was specified to subscribe to a Unix socket published by the harness,
and explicitly not to read files. That design predates the run directory being
what it is now: an append-only `events.jsonl` that is already the single source
of truth, already the thing `nih status` folds, and already written durably
before the harness acts on anything.

Building the socket would mean a broadcaster in the harness, a protocol beside
the log, and a viewer that works for live runs but not for finished ones.

## Decision drivers

- The viewer must work when no agent is involved; that is the user's condition
  for the management surface existing at all.
- "The TUI computes nothing" must stay true.
- Less machinery for the same result is worth a rule change.

## Considered options

1. **Follow the log: tail `events.jsonl` by byte offset and apply what is new.**
2. Build the socket broadcaster as originally specified.
3. Poll `nih status --json` as a subprocess.

## Decision outcome

Option 1. The viewer takes `--run <dir>`, reads the log from where it last
stopped, and applies only whole lines - a run writing a line while the viewer
reads must not have it parsed in halves. The same viewer renders a finished run
with no special case.

Option 3 is excluded by a rule that still holds: the TUI does not shell out to
Python.

Option 2 remains the answer for the case this does not cover - a viewer on
another machine, or inside a container without the run directory mounted - and
stays on the roadmap for when that case is real.

## Consequences

- CLAUDE.md §10 and §15 are out of date in the wording that says the TUI reads
  a socket and never reads files; they are corrected in the same change, as
  that file requires.
- The viewer gains the other half of the control plane: `c` appends a cancel to
  the run's inbox, exactly as `nih cancel` and an agent do. Approving a plan and
  answering questions are not in the TUI yet; they are on the roadmap.
- Latency is the poll interval rather than a push. At 400 ms for a view a human
  reads, that is not a cost anyone can perceive.

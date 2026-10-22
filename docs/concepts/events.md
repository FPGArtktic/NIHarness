<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Events

Run state is an append-only `events.jsonl` plus periodic snapshots. Nothing in
the harness mutates run state in place.

```json
{"seq":12,"ts":"2026-10-14T18:22:41.511Z","run_id":"8f21c0","step_id":"step-3",
 "body":{"type":"tool.called","call_id":"call_7","name":"read_file",
         "arguments":{"path":"src/net.c"},"arguments_digest":"3f9a4c1e2b7d8a05"}}
```

The envelope carries position and time; the body is one of the payloads in the
generated [event catalogue](../reference/events.md). Payloads are a discriminated
union on `type`, so adding an event cannot silently change an existing one.

## Who assigns what

| Field | Assigned by | Why |
|---|---|---|
| `body` | the domain | pure, deterministic, no clock |
| `seq`, `ts` | the event sink | the clock belongs at the edge |
| `run_id`, `step_id` | the sink, from context | correlation without threading them through the domain |

Appends are flushed and `fsync`ed before the call returns: the harness never
acts on an event it might lose.

## Derived, not stored

```mermaid
flowchart LR
  E[(events.jsonl)] -->|fold| S[RunSnapshot]
  E -->|view + compaction| W[Model window]
  E -->|socket| T[TUI]
  S -->|resume| R[Run loop]
```

`nih status` is exactly that fold, and so is the TUI's state. Snapshots are an
optimisation so a resume need not replay from zero - never a second source of
truth.

## Control is a request, not an event

A run is steerable from outside: a person at the TUI, an agent over MCP, a
script at the CLI. None of them appends to the log, and the reason is
mechanical - `JsonlEventSink` assigns sequence numbers in memory, so a second
writer would duplicate `seq` and the fold would reject the stream.

So control arrives through an **inbox**, `control.jsonl` beside the log:

```mermaid
flowchart LR
  TUI -->|c| INBOX[(control.jsonl)]
  AGENT[agent over MCP] -->|run_cancel| INBOX
  CLI[nih cancel] --> INBOX
  INBOX -->|drained between steps| RUN[the run]
  RUN -->|run.cancelled| LOG[(events.jsonl)]
```

Many writers, each submission one `O_APPEND` write the kernel keeps whole; one
reader, the run, with a cursor beside the file. The run turns a request into an
event when it acts on it, which keeps the log a record of what happened rather
than of what was asked for - and makes an ignored request visible as an inbox
line with no matching event.

A submitter may leave the message id empty; the inbox fills it in. That is what
lets the TUI write a control line with a JSON serialiser and nothing else.

## A run says which tools it could reach

A run that reaches an MCP server the harness did not write records it, once,
before the first task:

```json
{"seq":4,"ts":"...","run_id":"8f21c0","body":{"type":"tools.attached",
 "server":"fpga","tools":["read_timing_report"],"roles":["implementer"]}}
```

Without that line, a `tool.called` naming `fpga__read_timing_report` would be
unexplainable on replay: the catalogue is built at connect time and lives
nowhere in the log otherwise. The event carries the server's name and its
imported tools, not its command line - a command line is where a token hides,
and anyone who can read the run can read the log. See
[declared MCP servers](../operations/mcp-servers.md).

## Schemas

`nih docs generate` exports the JSON Schema of the envelope, every artifact and
every tool argument set to `schemas/`. That directory is the contract with the
Rust TUI, which checks it at build time, so a schema change that would break the
viewer fails the gate instead of the viewer.

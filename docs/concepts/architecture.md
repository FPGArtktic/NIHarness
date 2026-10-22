<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architecture

The harness is hexagonal, for one practical reason: the decisions worth testing
are the ones a model, a container or a network can make flaky. Keeping them in a
pure core means they are tested without any of those.

```mermaid
C4Context
  title NIHarness - containers
  Person(dev, "Developer", "Writes a spec, approves a plan, reads the TUI")
  System_Boundary(harness, "NIHarness") {
    Container(core, "Python core", "niharness", "Pipeline, gate, tool loop, compaction")
    Container(tui, "Rust TUI", "ratatui", "Renders the event stream over a Unix socket")
    ContainerDb(events, "Event log", "events.jsonl", "Append-only run state and snapshots")
  }
  System_Ext(llama, "llama-server", "OpenAI-compatible HTTP on localhost")
  System_Ext(bitnet, "bitnet.cpp", "Ternary utility model on CPU")
  System_Ext(sandbox, "Podman sandbox", "Tools, --network=none")
  System_Ext(qdrant, "Qdrant", "Code and run-context chunks")

  Rel(dev, tui, "Watches, answers questions, cancels")
  Rel(core, events, "Appends")
  Rel(tui, events, "Reads via socket")
  Rel(core, llama, "Chat completions, tokenize")
  Rel(core, bitnet, "Summaries, compaction")
  Rel(core, sandbox, "Executes validated tool calls")
  Rel(core, qdrant, "Indexes and queries chunks")
```

## The four layers

```mermaid
flowchart TD
  CLI[niharness.cli<br/>wires adapters] --> ADAPTERS[niharness.adapters<br/>httpx, MCP, git, Podman, Qdrant, JSONL]
  ADAPTERS --> PORTS[niharness.ports<br/>Protocols]
  PORTS --> DOMAIN[niharness.domain<br/>pure logic and contracts]
```

The rules, enforced by `uv run lint-imports`:

- `domain` imports nothing from `adapters`, `cli`, `config`, and no I/O library
  at all - not `httpx`, not `subprocess`, not even `socket`.
- `ports` depend on the domain only.
- Adapters do not import each other; the CLI composes them.

## Why the domain is pure, concretely

A pure domain is what lets these be plain unit tests:

| Decision | Tested with |
|---|---|
| Which gate commands run, in what order | a list of marker file names |
| Whether a tool call is allowed | a frozen allowlist and an argument dict |
| Whether a step is looping | a synthetic window of call records |
| What compaction drops | a window of items and a fake tokenizer |
| Where the pipeline goes next | a stage and an outcome |
| What a run's state is | a list of events |

No model, no container, no clock. The clock lives in the event sink, which is
also why replaying a log is deterministic: timestamps and sequence numbers are
data, not side effects.

## Who drives it

The harness is driven by whoever is there, through one surface:

```mermaid
flowchart TD
  CLAUDE[agent over MCP] --> MGMT[niharness.management]
  TUI[Rust TUI] --> INBOX[(control inbox)]
  CLI[nih runs, status, cancel] --> MGMT
  MGMT --> INBOX
  MGMT --> LOG[(events.jsonl)]
  INBOX --> RUN[the run]
  RUN --> LOG
  LOG --> TUI
```

`niharness.management` holds the registry, the plan store and the bounded views;
`niharness.cli` and the MCP server in `niharness.management.server` are two
callers that know nothing about each other. The layer may not import the CLI or
the tool server, and `lint-imports` checks that.

What that surface deliberately cannot do is write code, commit or run the gate -
see [ADR 0016](../adr/0016-the-management-surface.md), and
[Managing from an agent](../operations/managing-from-claude.md) for the
reasoning in full.

## Adding a subsystem

A new subsystem is six things in one change: a port Protocol, an adapter, a
contract test with recorded responses, a schema export if it introduces an
artifact, a documentation page, and an ADR. Anything less leaves a hole someone
else has to find.

See [ADR 0002](../adr/0002-hexagonal-architecture.md).

<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Architecture decision records

Every architectural decision is recorded here in [MADR](https://adr.github.io/madr/)
form, numbered, and never edited after acceptance - a decision that changes gets
a new record that supersedes the old one, so the reasoning stays recoverable.

Read these before changing a port, adding a dependency, or changing the shape of
the pipeline. A change that does one of those without an ADR is incomplete.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-hexagonal-architecture.md) | Hexagonal architecture with a pure domain | Accepted |
| [0003](0003-event-sourced-run-state.md) | Run state is an append-only event log | Accepted |
| [0004](0004-openai-compatible-inference-port.md) | One inference protocol, localhost only | Accepted |
| [0005](0005-commit-is-the-gate.md) | `git_commit` is the quality gate | Accepted |
| [0006](0006-compaction-as-a-view-policy.md) | Compaction is a view policy, not a log edit | Accepted |
| [0007](0007-gpl-3-0-or-later.md) | GPL-3.0-or-later for the whole repository | Accepted |
| [0008](0008-sandbox-is-one-container-per-command.md) | One hardened container per command | Accepted |
| [0009](0009-the-tool-loop-is-a-pure-state-machine.md) | The tool loop decides, the executor acts | Accepted |
| [0010](0010-an-application-layer.md) | An application layer between the CLI and the ports | Accepted |
| [0011](0011-message-assembly-is-an-application-concern.md) | Message assembly is an application concern | Accepted |
| [0012](0012-mcp-is-the-tool-protocol.md) | MCP is the tool protocol, and the first runtime dependency | Accepted |
| [0013](0013-tools-answer-with-an-outcome-envelope.md) | This project's tools answer with an outcome envelope | Accepted |
| [0014](0014-review-starts-from-the-diff.md) | A step declares its artifact; review starts from the diff | Accepted |
| [0015](0015-control-actions-are-requests.md) | Control actions are requests, delivered through an inbox | Accepted |
| [0016](0016-the-management-surface.md) | The management surface: full control, no code path | Accepted |
| [0017](0017-the-tui-follows-the-event-log.md) | The TUI follows the event log (supersedes the socket) | Accepted |
| [0018](0018-a-run-records-its-conclusions.md) | A run records its conclusions, so it can be continued | Accepted |
| [0019](0019-declared-mcp-servers.md) | A project may declare MCP servers, under a namespace | Accepted |
| [0020](0020-language-servers-in-the-sandbox.md) | Language servers run in the sandbox, and are asked by name | Accepted |
| [0021](0021-retrieval-runs-in-the-harness.md) | Retrieval runs in the harness, over the store's REST API | Accepted |
| [0022](0022-an-edit-located-by-its-own-text.md) | An edit is located by its own text; the harness writes the diff | Accepted |
| [0023](0023-the-sandbox-network-is-a-deployment-choice.md) | The sandbox's network is a deployment choice, not a constant | Accepted |
| [0024](0024-a-benchmark-layer-is-scored-by-folding-the-event-log.md) | A benchmark layer is scored by folding the event log | Accepted |
| [0025](0025-creation-is-an-edit-with-nothing-to-find.md) | Creation is an edit with nothing to find (extends 0022) | Accepted |

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by ADR-MMMM
- Date: YYYY-MM-DD

## Context and problem statement
## Decision drivers
## Considered options
## Decision outcome
## Consequences
```

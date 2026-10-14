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

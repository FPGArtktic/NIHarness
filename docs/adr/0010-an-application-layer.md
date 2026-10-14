<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0010. An application layer between the CLI and the ports

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

[ADR 0009](0009-the-tool-loop-is-a-pure-state-machine.md) left the loop pure
and created something that has to perform its decisions: call the model, run
the tool, roll back the worktree, append the events. That code is not domain
logic - it awaits ports - and it is not an adapter either, because it
implements no port and belongs to no protocol.

Putting it in `cli/` was the obvious shortcut, and the reason not to: the CLI
is supposed to parse arguments and wire adapters, and anything living there
becomes reachable only through a command invocation.

## Decision drivers

- A step must be runnable from a test, from `nih implement`, from a sub-run and
  eventually from the TUI socket, without going through argument parsing.
- It must depend on ports only, so it never knows which backend or sandbox it
  is driving.
- The dependency direction must stay checkable by `lint-imports`.

## Considered options

1. **A `niharness.application` package between the CLI and the ports.**
2. The executor inside `niharness.cli`.
3. The executor as a method on an adapter.

## Decision outcome

Option 1, with two new contracts:

- `cli` → `application` → `ports` → `domain` is a layers contract.
- `application` may not import `adapters`, `cli`, `httpx`, `subprocess` or
  `qdrant_client`: it drives ports and nothing else.

Adapters and the application layer are siblings; the CLI composes them.

## Consequences

- `StepRunner` is tested with four fake ports and no I/O at all, and two of
  those fakes satisfy their Protocols structurally - which is itself a check
  that the Protocols are implementable.
- Retries, timeouts and anything else operational belong here, not in the
  domain and not in an adapter.
- One more layer to explain. The check makes the rule enforceable rather than
  cultural, which is the trade this project keeps making.

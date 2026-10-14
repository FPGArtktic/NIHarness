<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0002. Hexagonal architecture with a pure domain

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

The interesting logic of this harness - which gate command runs, whether a tool
call is allowed, whether a step is looping, what compaction drops, where the
pipeline goes next - is exactly the logic that becomes untestable if it is
tangled with HTTP, containers, a vector store and a clock. A harness whose own
decisions cannot be tested has no business gating anyone else's code.

## Decision drivers

- Those decisions must be unit-testable with no model, no container, no network
  and no filesystem.
- Replaying an event log must be deterministic, which forbids reading a clock in
  the core.
- Backends are expected to change (`llama-server` today, something else later);
  that churn must not reach the core.

## Considered options

1. **Hexagonal: pure `domain`, Protocol `ports`, swappable `adapters`, wiring in
   `cli`.**
2. A service-layer architecture with dependency injection but no purity rule.
3. Pragmatic layering by convention, enforced in review.

## Decision outcome

Option 1, with the boundary enforced mechanically by `import-linter`:

- `niharness.domain` may not import `adapters`, `cli`, `config`, or any I/O
  library - not `httpx`, not `subprocess`, not `socket`.
- `niharness.ports` depends on the domain only.
- Adapters never import each other.
- `niharness.cli` is the only place that composes them.

Where the core needs a capability that looks like I/O, it takes a function: the
compaction policy takes a `measure` callable rather than a tokenizer client.

## Consequences

- Timestamps and sequence numbers are assigned by the event sink, not the core.
  The core is a function of its inputs.
- A new subsystem is six artifacts in one change: port, adapter, contract test,
  schema export (if it adds an artifact), docs page, ADR.
- Some indirection exists that a monolith would not need - a Protocol plus an
  adapter for something as small as reading a file in a sandbox.
- `uv run lint-imports` can fail for a structural reason that looks unrelated to
  the change at hand. That is the check doing its job.

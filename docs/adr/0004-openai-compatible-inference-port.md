<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0004. One inference protocol, localhost only

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

The harness drives at least two very different runtimes: `llama-server` for the
coder model and `bitnet.cpp` for the ternary utility model. Both expose
OpenAI-compatible HTTP. The temptation is a backend abstraction with per-backend
branches; that is how a codebase acquires an `if ollama:` that nobody dares
delete.

## Decision drivers

- Air gap: the runtime must not be able to reach a remote provider, even by
  misconfiguration.
- Capability differences (GBNF grammars, reasoning content) must live in
  configuration, not in control flow.
- Contract tests must run with no backend at all.

## Considered options

1. **One `ModelBackend` port speaking OpenAI-compatible HTTP, with capability
   flags in configuration.**
2. A backend abstraction with per-runtime adapters and feature detection.
3. In-process bindings (`llama-cpp-python`).

## Decision outcome

Option 1. A single adapter, `OpenAiCompatBackend`, which also implements the
`Tokenizer` port via `/tokenize`. Capability flags (`supports_grammar`,
`supports_reasoning`) come from the profile; a grammar is simply withheld from a
backend configured as unable to use one.

The base URL is validated at construction: a non-loopback host raises
`AirGapViolationError`. There is no flag to disable that check.

Token counts come from the backend. `count_messages` renders through the
backend's chat template (`/apply-template`) and tokenises the result, so the
number matches what the model will actually see.

## Consequences

- No backend-specific branches, and `--network=none` is not the only thing
  standing between the harness and the internet.
- Streaming is not used by the core loop; a response is parsed as a whole, and
  degenerate-output detection therefore applies to the completed stream.
- A backend that deviates from the protocol is reported as
  `BackendProtocolError` instead of being accommodated.
- In-process inference would be faster by one copy; it would also put a C++
  runtime inside the harness process. Not worth it.

<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0013. This project's tools answer with an outcome envelope

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

MCP has an error channel: a result carries `isError`, and a server that raises
produces one. Writing this project's own tool server showed what that costs
here: the SDK masks the exception's detail, so a failing call came back as
`Error executing tool run_tests`.

The detail is the entire value of the answer. A red gate's output is what the
implementer has to read to fix the cause, and `git_commit` refusing with "the
gate is red" and nothing else would be a harness that tells the model to guess.

The harness also needs the failure *flag* to be reliable, not only readable: the
executor derives a gate signature from a red result, and the thrash detector
counts those signatures. A failure that arrives as a successful call with sad
text in it would make a looping step invisible.

## Decision drivers

- The model must receive the full diagnostic text of a failure.
- The harness must be able to tell success from failure mechanically.
- A server that is not this project's own must still work.

## Considered options

1. **An outcome envelope: this project's servers return
   `{"ok": bool, "text": str}` as structured content; the client prefers it and
   falls back to the protocol's own signal for any other server.**
2. Raise in the server and accept the masked message.
3. Return the detail as plain text and let the model infer success from reading
   it.

## Decision outcome

Option 1. Every tool in `niharness.toolserver` returns an `Outcome`, which the
SDK publishes as structured content; `McpToolProvider` reads `ok` and `text`
from it when both are present and well typed, and otherwise falls back to
`isError` plus the content blocks.

Option 3 was rejected because it is exactly the failure mode this project is
built against: inferring a result from prose instead of a signal. Option 2
throws away the only thing worth returning.

## Consequences

- Two servers can be talked to at once: a tool server of this project's, and any
  conformant MCP server, read the ordinary way.
- The envelope is a convention, not an extension of the protocol: it travels in
  structured content, which MCP defines, and a client that ignores it still
  sees the JSON text.
- A tool that *crashes* rather than returning still goes through the protocol's
  error channel with a masked message. That is the loud, rare case, and it is
  distinguishable from a refusal.
- The gate's refusal is now a first-class result: `git_commit` comes back with
  `ok: false` and the failing command's output, and the executor turns that into
  the gate signature the thrash detector compares.

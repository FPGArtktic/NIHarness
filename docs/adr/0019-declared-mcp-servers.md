<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0019. A project may declare MCP servers, under a namespace

- Status: Accepted
- Date: 2026-10-22

## Context and problem statement

The harness already speaks MCP in three places: it is a client of the tool
server in the sandbox (ADR 0012), it *is* a tool server there, and it is a
management server for an agent that drives it (ADR 0016). What it could not do
is use anybody else's.

That is a real gap rather than a nice-to-have. A vendor toolchain behind an MCP
server is the only sanctioned way to reach some tools at all - this project's
own gate matrix already says Vivado TCL goes through the FPGA MCP server - and
a local indexer or documentation service is exactly the kind of help a 30B
model needs and the harness cannot provide itself.

The obstacle was identity. `ToolName` was a closed enum, every tool had a
Pydantic argument model, and `validate_call` consulted both. A tool the harness
has never heard of could not be addressed, described, validated or logged.

## Decision drivers

- An external server must not be able to widen what a role may do, and must
  not be mistakable for the harness's own tools - above all for `git_commit`,
  which is the gate.
- The model is a local 30B; it needs each tool's own schema, not one generic
  "call anything" tool it has to guess the arguments of.
- Every call still has to be validated before it is sent, and every call and
  result still has to be an event.
- Nothing may become implicit. The air-gap and the sandbox are the premises of
  this project; a feature that starts processes must say which, and why.

## Considered options

1. **Declared servers, namespaced tools, schema-checked arguments.** A server
   exists because `harness.toml` names it; its tools are imported by name,
   granted to named roles, and addressed as `<server>__<tool>`.
2. One built-in meta-tool, `mcp_call(server, tool, arguments)`.
3. Proxy external servers through the sandbox tool server, so the harness still
   sees one server.
4. Discover whatever a configured server offers and forward all of it.

## Decision outcome

Option 1.

Tool identity widens from the closed `ToolName` enum to `ToolId`: a built-in
name, or `<server>__<tool>`. No built-in name contains the separator, so the
namespace cannot be squatted; a server that calls its tool `git_commit`
contributes `fpga__git_commit`, and the gate stays the sandbox's. The
`ToolCatalog` - external tools, their advertised schemas, and the roles granted
them - is built at connect time and carried on `LoopConfig`, so the domain
validates an external call as purely as it validates `apply_patch`.

Arguments cannot be validated against a Pydantic model the harness does not
have, so they are checked against the schema the server advertised, in the
subset `check_arguments` understands: `type`, `enum`, `required`,
`properties`, `additionalProperties: false`, `items`. Formats, bounds and the
combinators are left to the server, which is the only side that knows what its
tool means. The check is not a validator; its job is to turn a malformed call
into a refusal the model can read instead of a transport error it cannot.

There is no path confinement on an external call, because the harness cannot
confine arguments whose meaning it does not know. What bounds the feature is
everything upstream: a human wrote the server into a committed file, named the
tools to import, and named the roles that get them. A declared tool the server
does not offer stops the run rather than narrowing it silently.

Option 2 keeps the enum closed and the diff small, at the cost of the only
thing that makes a weak model usable with an unfamiliar tool: its schema.
Option 3 moves the problem into the sandbox without solving it - the harness
still has to know the names to allow and log them. Option 4 lets a server
decide what a role may do, which is the one thing that must stay with the
configuration.

## Consequences

- `AcceptedCall.tool` and `ToolResult.tool` are `ToolId` strings rather than
  `ToolName` members. The enum remains the built-in vocabulary, and comparing a
  result against it still works because it is a `StrEnum`; `.value` on a
  validated call does not, and every site was changed.
- Where a declared server runs is the configuration's decision, not the
  harness's: `command` may be a host binary or `podman run --network=none ...`.
  This is the one place where a tool can run outside the sandbox, so
  `nih implement` says so on stderr when any server is declared, and the run
  records a `tools.attached` event naming the server, its imported tools and
  the roles granted them. The event carries the name and not the command line,
  because a command line is where a token hides.
- `nih tools` starts every declared server and prints what came back, so a
  server that would fail during a run fails before one.
- The gate is untouched. An external tool cannot commit, cannot run the gate,
  and a change still becomes a commit only by passing the gate in the sandbox.
- A second process on the other end of a pipe exposed something the in-process
  tests could not: an MCP stdio session runs inside two anyio task groups, and
  each wraps whatever failed inside it in a `BaseExceptionGroup`, so a CLI
  `except` clause written against the failure never fired. The adapter now
  unwraps a single-failure group before it leaves, and the CLI does the same
  where it starts the event loop (`cli/blocking.py`). This was already true of
  the harness's own tool session: a backend error raised while the session was
  open reached `nih implement` wrapped, past its handlers.

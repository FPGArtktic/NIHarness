<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0012. MCP is the tool protocol, and the first runtime dependency to prove it

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

The executor needs tools. The `ToolProvider` port says what the harness needs -
advertise schemas for an allowlist, execute a validated call, return a
truncated result - and says nothing about how. Something has to speak a wire
protocol to a server that runs inside the sandbox.

This is also the first time the harness takes a dependency for a runtime code
path, so the decision is two decisions: which protocol, and whether to depend on
someone's implementation of it.

## Decision drivers

- Tool servers must be replaceable without touching the harness: a Vivado TCL
  server, a Yocto server and a plain filesystem server should all plug in.
- The protocol has to work over stdio, because that is what survives
  `--network=none`.
- Every dependency must be air-gappable (a wheel in the local mirror) and
  GPLv3-compatible.
- Contract tests must run with no server, no container and no network.

## Considered options

1. **MCP, using the official Python SDK (`mcp`), as a runtime dependency.**
2. A minimal in-house JSON-RPC-over-stdio protocol, no dependency.
3. Tools as plain subprocesses through the `Sandbox` port, no protocol at all.

## Decision outcome

Option 1. The SDK is MIT, pure Python, and installs from the mirror like
anything else; it is recorded in `THIRD_PARTY.md` reporting and checked by
`pip-licenses` in the full gate.

Option 3 is what the gate already does for its own commands, and it is fine for
running a fixed argv. It is not fine for tools: every server would have to
re-implement argument schemas, error shapes and result framing, and the model
would see a different contract per server.

Option 2 is the tempting one - the harness needs perhaps four JSON-RPC methods.
It was rejected because the protocol is not the hard part; the ecosystem is. An
in-house protocol means every tool server in this project is written by this
project, forever.

The adapter is deliberately suspicious of what it talks to:

- a server may advertise anything; only tools the harness knows **and** the
  role's allowlist permits are forwarded to the model, so a server cannot widen
  a role's permissions by advertising `shell`,
- the arguments sent are the ones the domain already validated, never the
  model's raw proposal,
- results are flattened to text, truncated to the configured cap, and a
  transport failure becomes a red `ToolResult` rather than an exception that
  would take the step down.

## Consequences

- One runtime dependency, with a transitive tail (anyio, httpx, pydantic,
  starlette, uvicorn). `uv.lock` pins it with hashes and the wheelhouse has to
  be rebuilt; the full gate checks the licenses.
- The SDK's API moves: the high-level server class and the registration
  decorators changed names between the versions this project looked at. The
  lock file is what keeps that from being a surprise, and the contract tests
  are the early warning.
- Launching the server is the caller's business: `connect_stdio` takes the
  command, which in the shipped wiring is `podman run -i` with the hardening
  flags of [ADR 0008](0008-sandbox-is-one-container-per-command.md). That server
  container is long-lived, unlike the per-command containers the gate uses,
  because a stdio session has state. The isolation flags are identical.
- Contract tests run a real server in-process over the SDK's in-memory streams,
  so the protocol is exercised without a subprocess.

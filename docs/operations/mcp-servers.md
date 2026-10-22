<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Declared MCP servers

The harness ships one tool server: the one in the sandbox that reads, patches,
runs the gate and commits. A project can declare more. They are MCP servers the
harness did not write - a vendor toolchain, a local indexer, a documentation
service - and their tools become available to the roles you name.

This is the only place in the harness where a tool can run outside the sandbox,
so it is worth reading the whole page before adding one.

## Declaring one

```toml
# harness.toml
[[mcp.servers]]
name = "fpga"                               # (1)!
command = "/opt/fpga-mcp/bin/server"
args = ["--stdio"]
env = { XILINX_VIVADO = "/opt/Xilinx/Vivado/2024.1" }  # (2)!
tools = ["read_timing_report", "list_runs"]  # (3)!
roles = ["implementer", "reviewer"]          # (4)!
timeout_seconds = 300
```

1. The namespace. Its tools appear to the model as `fpga__read_timing_report`.
   No built-in tool name contains `__`, so nothing a server advertises can be
   mistaken for one - a server that calls its own tool `git_commit` contributes
   `fpga__git_commit`, and the gate stays the sandbox's.
2. Written out, not inherited. An empty table means the minimal environment the
   MCP SDK provides, which is a `PATH` and little else.
3. The import list. A server advertising twenty tools and asked for two
   contributes two. Nothing is implicit.
4. The grant. Roles not named here never see the tools, so a reviewer can be
   given a read-only vendor tool while the implementer gets more.

Both `tools` and `roles` are required: an entry that grants nothing is a
mistake, and the configuration refuses it at load time rather than at run time.

## Checking it before a run

```bash
uv run nih tools
```

This starts every declared server, asks it what it offers, and prints the
result against the roles that were granted it - alongside the built-in tools,
per role, so you can see the whole surface at once. A server that would fail
during a run fails here instead, with the same message and nothing half-done:

```
the MCP server 'fpga' does not offer place; it offers list_runs, read_timing_report
```

`nih tools --json` is the same thing for a script, and `config_show` over
`nih mcp` names the declared servers too, so a supervising agent knows what a
run can reach.

## What the harness does with a declared call

Every call goes through the same validation as a built-in one, with one
difference: there is no argument model, because the harness does not have one.

| Check | Built-in tool | Declared tool |
|---|---|---|
| the tool exists | the `ToolName` enum | the import list |
| the role may use it | the role's allowlist | the `roles` grant |
| the arguments fit | a Pydantic model | the schema the server advertised |
| paths stay in the worktree | yes | **no** - see below |
| size caps | diff size | `tools.max_argument_bytes` |
| result truncation | yes | yes |
| the call and its result are events | yes | yes |

The advertised schema is checked in the subset the harness understands:
`type`, `enum`, `required`, `properties`, `additionalProperties: false` and
`items`. Formats, numeric bounds and the combinators (`oneOf`, `anyOf`) are
left to the server, which is the only side that knows what its tool means. The
point of the check is not to be a JSON Schema validator: it is to turn an
obviously malformed call into a refusal the model can read, instead of a
transport error it cannot.

There is **no path confinement** on a declared tool's arguments, because the
harness cannot confine arguments whose meaning it does not know. What bounds
the feature is everything upstream of the call: a human wrote the server into a
committed file, named the tools, and named the roles.

## Where it runs

`command` decides, and the harness does not second-guess it:

=== "On the host"

    ```toml
    command = "/opt/fpga-mcp/bin/server"
    ```

    The server runs as this user, with whatever this user can reach. Use it
    when the tool genuinely needs the host - a licence server, a USB
    programmer, a vendor installation.

=== "In a container"

    ```toml
    command = "podman"
    args = ["run", "--rm", "-i", "--network=none",
            "--volume", "/opt/vendor:/opt/vendor:ro",
            "localhost/fpga-mcp:latest"]
    ```

    The same hardening as the harness's own sandbox, chosen by configuration
    rather than by code. Prefer this.

!!! warning "A declared server is not sandboxed by the harness"

    `nih implement` says so on stderr whenever a server is declared, and the
    run's event log records a `tools.attached` event naming the server, the
    tools imported from it, and the roles granted them. The event carries the
    server's name and not its command line, because a command line is where a
    token hides; the command goes to the structured log with the rest of the
    wiring.

## What a declared server still cannot do

- **Commit.** `git_commit` is in the sandbox's tool server and nowhere else. A
  change becomes a commit by passing the gate, and that is the only route.
- **Run the gate**, or weaken it.
- **Widen a role.** The configuration grants; the server only answers.
- **Appear without being named.** There is no discovery, no default server, and
  no "available tools" list the harness would pick up on its own.

## When a server is missing

A declared server that cannot be started, or that does not offer a tool the
configuration imports, stops the run before any step begins, with exit code 69
(`EX_UNAVAILABLE`). That is deliberate: the roles' permissions were written
down, and a run that quietly drops one is not the run that was asked for.

```bash
uv run nih tools                 # which server, and what it actually offers
uv run nih implement             # refuses early, for the same reason
```

The decision and the alternatives that were weighed are in
[ADR 0019](../adr/0019-declared-mcp-servers.md).

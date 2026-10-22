# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih tools`: what the models can call, and which role may call it.

A declared MCP server is part of what a role is allowed to do, so it should be
checked the way the rest of the setup is checked - before a run, on purpose.
This command starts every server named in `harness.toml`, asks it what it
offers, and prints the result against the roles that were granted it. If a
server will fail during a run, it fails here instead, with the same message and
nothing half-done.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

import typer
from mcp import StdioServerParameters

from niharness.adapters.mcp import ExternalServer, ExternalToolsError, attach
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.config import HarnessConfig
from niharness.domain.roles import DEFAULT_ROLES
from niharness.domain.tools import EMPTY_CATALOG, ExternalTool, ToolCatalog

EX_UNAVAILABLE = 69
"""`sysexits.h`: a service the command needs is not there."""


def external_servers(config: HarnessConfig) -> tuple[ExternalServer, ...]:
    """Translate the declared servers into what the adapter needs.

    The adapter is told a name, how to start a process, which tools to import
    and which roles get them. Where that came from is the CLI's business.
    """
    return tuple(
        ExternalServer(
            name=server.name,
            parameters=StdioServerParameters(
                command=server.command,
                args=list(server.args),
                # An empty environment would leave the server without a PATH;
                # None is what the SDK reads as "the usual minimal one".
                env=dict(server.env) or None,
            ),
            tools=frozenset(server.tools),
            roles=frozenset(role.value for role in server.roles),
            timeout_seconds=server.timeout_seconds,
        )
        for server in config.mcp.servers
    )


async def discover(config: HarnessConfig) -> ToolCatalog:
    """Start every declared server and return what they contribute.

    Raises:
        ExternalToolsError: if a server cannot be started or does not offer a
            tool the configuration imports.
    """
    servers = external_servers(config)
    if not servers:
        return EMPTY_CATALOG
    async with AsyncExitStack() as stack:
        external: list[ExternalTool] = []
        for server in servers:
            tools = await stack.enter_async_context(attach(server, limits=config.tools))
            external.extend(await tools.discover())
        return ToolCatalog(external=tuple(external))
    raise AssertionError("unreachable: the context manager always returns")  # pragma: no cover


def _payload(config: HarnessConfig, catalog: ToolCatalog) -> dict[str, Any]:
    """Return the machine-readable form: built-in tools per role, then servers."""
    return {
        "ok": True,
        "builtin": {
            role.value: sorted(tool.value for tool in spec.allowlist)
            for role, spec in sorted(DEFAULT_ROLES.items())
        },
        "servers": [
            {
                "name": server,
                "command": next(
                    declared.command
                    for declared in config.mcp.servers
                    if declared.name == server
                ),
                "roles": list(catalog.roles_of(server)),
                "tools": [
                    {"id": tool.id, "name": tool.name, "description": tool.description}
                    for tool in catalog.external
                    if tool.server == server
                ],
            }
            for server in catalog.servers()
        ],
    }


def _human(config: HarnessConfig, catalog: ToolCatalog) -> str:
    """Return the operator's form: who may call what, and from where."""
    lines = ["built-in, in the sandbox (the gate among them):"]
    for role, spec in sorted(DEFAULT_ROLES.items()):
        offered = ", ".join(sorted(tool.value for tool in spec.allowlist)) or "none"
        lines.append(f"  {role.value:<13} {offered}")
    if not config.mcp.servers:
        lines.append("")
        lines.append("no MCP servers declared; see docs/operations/mcp-servers.md to add one")
        return "\n".join(lines)
    lines.append("")
    lines.append("declared MCP servers:")
    for server in catalog.servers():
        command = next(d.command for d in config.mcp.servers if d.name == server)
        lines.append(f"  {server} ({command}) -> {', '.join(catalog.roles_of(server))}")
        for tool in catalog.external:
            if tool.server == server:
                lines.append(f"      {tool.id:<28} {tool.description[:48]}")
    return "\n".join(lines)


def tools(config: HarnessConfig, *, json_mode: bool) -> None:
    """Print the tools a run would offer, starting the declared servers to ask.

    Raises:
        typer.Exit: when a declared server cannot be reached.
    """
    try:
        catalog = blocking(discover(config))
    except ExternalToolsError as error:
        fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
        raise typer.Exit(EX_UNAVAILABLE) from error
    emit(_payload(config, catalog), json_mode=json_mode, human=_human(config, catalog))

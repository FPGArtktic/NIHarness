# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools from MCP servers the harness did not write.

The harness ships one tool server, the one in the sandbox that holds the gate.
A project often needs more than that - a vendor toolchain behind an MCP server,
an indexer, a documentation service - and those servers already speak the
protocol the harness speaks. This module lets a project declare them and makes
their tools available to named roles, under four rules:

* **nothing is implicit.** A server exists only because a human wrote it into
  `harness.toml`, and only the tools named there are imported. A server that
  advertises twenty tools and is asked for two contributes two.
* **a declared tool that is missing is fatal.** The roles' permissions were
  written down; losing one silently would change what the run is allowed to do
  without anyone noticing.
* **the name is namespaced.** Every imported tool is addressed as
  `<server>__<tool>`, so no server can be mistaken for the harness's own tools,
  and the event log says which server answered.
* **the gate is untouched.** External tools cannot commit; a change still
  becomes a commit only by passing the gate inside the sandbox.

Where such a server runs is the configuration's decision, not this module's:
`command` may be a host binary, or `podman run --network=none ...`. The harness
reports what it started either way, and `nih tools` is how an operator checks
before a run rather than during one.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from niharness.adapters.mcp.provider import (
    connect_stdio,
    envelope_of,
    render_content,
    result_summary,
    transport_failure,
)
from niharness.domain.messages import ToolSchema
from niharness.domain.tools import (
    HARNESS_SERVED_TOOLS,
    NAMESPACE,
    AcceptedCall,
    ExternalTool,
    ToolCatalog,
    ToolId,
    ToolLimits,
    ToolResult,
    is_external,
    truncate_result,
)
from niharness.ports.tool_provider import LocalToolProvider, ToolProvider

_log = structlog.get_logger(__name__)


class ExternalServer(BaseModel):
    """One declared MCP server, as the wiring hands it to this adapter.

    This is not the configuration model: the CLI reads `harness.toml` and
    translates. The adapter is told a name, how to start a process, which tools
    to import and which roles get them, and nothing about where that came from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    parameters: StdioServerParameters
    tools: frozenset[str] = Field(min_length=1)
    roles: frozenset[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0)


class ExternalToolsError(RuntimeError):
    """A declared MCP server could not be used as declared."""


class ServerUnavailableError(ExternalToolsError):
    """A declared server could not be started or did not complete a handshake."""

    def __init__(self, server: str, detail: object) -> None:
        self.server = server
        super().__init__(f"the MCP server {server!r} could not be started: {detail}")


class ToolNotOfferedError(ExternalToolsError):
    """A server started, but does not offer a tool the configuration imports."""

    def __init__(self, server: str, missing: Sequence[str], offered: Sequence[str]) -> None:
        self.server = server
        self.missing = tuple(missing)
        super().__init__(
            f"the MCP server {server!r} does not offer {', '.join(missing)}; "
            f"it offers {', '.join(offered) or 'nothing'}",
        )


class ExternalTools:
    """One external server's tools, addressed by their namespaced ids."""

    def __init__(
        self,
        server: ExternalServer,
        session: ClientSession,
        *,
        limits: ToolLimits | None = None,
    ) -> None:
        """Bind an initialised session to the declaration it was started from.

        Args:
            server: the declaration: name, imported tools, granted roles.
            session: a session whose `initialize` has already completed.
            limits: result caps; the shipped defaults when omitted.
        """
        self._server = server
        self._session = session
        self._limits = limits or ToolLimits()

    async def discover(self) -> tuple[ExternalTool, ...]:
        """Return the declared tools, with the schemas the server advertises.

        Returns:
            One :class:`~niharness.domain.tools.ExternalTool` per imported
            tool, in a stable order.

        Raises:
            ToolNotOfferedError: if the server does not offer a declared tool,
                or names one the harness cannot address.
        """
        listing = await self._session.list_tools()
        offered = {tool.name: tool for tool in listing.tools}
        missing = sorted(self._server.tools - set(offered))
        if missing:
            raise ToolNotOfferedError(self._server.name, missing, sorted(offered))
        imported: list[ExternalTool] = []
        for name in sorted(self._server.tools):
            advertised = offered[name]
            try:
                imported.append(
                    ExternalTool(
                        server=self._server.name,
                        name=name,
                        description=advertised.description or name,
                        parameters=dict(advertised.input_schema),
                        roles=self._server.roles,
                    ),
                )
            except ValidationError as error:
                raise ToolNotOfferedError(self._server.name, [name], sorted(offered)) from error
        _log.info(
            "tools.attached",
            server=self._server.name,
            command=self._server.parameters.command,
            tools=[tool.name for tool in imported],
            roles=sorted(self._server.roles),
        )
        return tuple(imported)

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Execute a validated call, under the name the server knows it by."""
        arguments: dict[str, Any] = accepted.args.model_dump(mode="json")
        remote = accepted.tool.split(NAMESPACE, 1)[1]
        try:
            raw = await self._session.call_tool(
                remote,
                arguments,
                read_timeout_seconds=self._server.timeout_seconds,
            )
        except MCPError as error:
            return transport_failure(accepted, error)
        ok, text = _read(raw)
        content, truncated = truncate_result(text, self._limits)
        return ToolResult(
            call_id=accepted.call_id,
            tool=accepted.tool,
            ok=ok,
            content=content,
            truncated=truncated,
            summary=result_summary(accepted, text, arguments),
        )


def unwrap_group(error: BaseException) -> BaseException:
    """Return the real failure inside nested single-error task groups.

    The MCP client runs its transport in anyio task groups, and a task group
    re-raises what happened inside it wrapped in a `BaseExceptionGroup` - twice
    over for a stdio session, once for the session and once for the transport.
    Callers write their `except` clauses against the failure, not against the
    plumbing, so the plumbing is peeled off here. A group holding more than one
    failure is returned as it is: there is no single answer to unwrap to.
    """
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _read(raw: types.CallToolResult) -> tuple[bool, str]:
    """Return whether a foreign server's result is green, and its text."""
    envelope = envelope_of(raw)
    if envelope is not None:
        return envelope
    return not raw.is_error, render_content(raw)


class CompositeToolProvider:
    """The harness's own tool server, plus every server a project declared.

    Calls route by name, over three destinations. Anything namespaced goes to
    the server that owns the namespace. A name the harness serves in its own
    process goes there - today that is `rag_query` alone, because the sandbox
    runs `--network=none` and the vector store is a service on the host.
    Everything else goes to the sandbox, which is the primary and cannot be
    shadowed: a built-in name has no namespace to claim, and a local provider
    declares the handful of names it answers rather than being asked.
    """

    def __init__(
        self,
        primary: ToolProvider,
        external: Mapping[str, ExternalTools],
        catalog: ToolCatalog,
        local: LocalToolProvider | None = None,
    ) -> None:
        """Wire the router.

        Args:
            primary: the sandboxed tool server, which holds the gate.
            external: the attached servers, by name.
            catalog: what those servers contributed, for the model's tool list
                and for the domain's validation.
            local: a provider running in the harness process, for the tools
                the sandbox cannot reach. None leaves those tools unoffered,
                which is what happens when the vector store is not configured.
        """
        self._primary = primary
        self._external = dict(external)
        self._catalog = catalog
        self._local = local
        self._served = local.served if local is not None else frozenset()

    @property
    def catalog(self) -> ToolCatalog:
        """Return the external tools this provider can reach."""
        return self._catalog

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        """Return the allowed schemas: the sandbox's, then the local, then the declared."""
        own = await self._primary.descriptors(allowlist)
        if self._local is not None:
            own += await self._local.descriptors(allowlist)
        external = tuple(
            ToolSchema(
                name=tool.id,
                description=tool.description or tool.id,
                parameters=dict(tool.parameters),
            )
            for tool in self._catalog.external
            if tool.id in allowlist
        )
        return own + tuple(sorted(external, key=lambda schema: schema.name))

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Execute a validated call on whichever server owns its name."""
        if accepted.tool in self._served and self._local is not None:
            return await self._local.call(accepted)
        if accepted.tool in _HARNESS_NAMES:
            # Allowlisted, and nothing is serving it. Sending it to the
            # sandbox would come back as "the tool server failed to run
            # rag_query", which blames the tool server for a fact about this
            # run's configuration - and the prompts tell the model the tool
            # exists, so this is reachable whenever retrieval is off.
            return _not_configured(accepted)
        if not is_external(accepted.tool):
            return await self._primary.call(accepted)
        server = accepted.tool.split(NAMESPACE, 1)[0]
        tools = self._external.get(server)
        if tools is None:  # pragma: no cover - validation refuses an unknown name first
            return transport_failure(accepted, f"no server {server!r} is attached")
        return await tools.call(accepted)


_HARNESS_NAMES: frozenset[ToolId] = frozenset(
    ToolId(tool.value) for tool in HARNESS_SERVED_TOOLS
)
"""The same set as the domain's, as tool ids, for routing."""


def _not_configured(accepted: AcceptedCall) -> ToolResult:
    """Return the refusal for a tool this run has nothing to serve with."""
    _log.info("tool.not_configured", tool=accepted.tool)
    return ToolResult(
        call_id=accepted.call_id,
        tool=accepted.tool,
        ok=False,
        content=(
            f"{accepted.tool} is not available in this run. It is served by the harness "
            "rather than the sandbox, and this run has no vector store or no embedding "
            "model configured, so there is nothing to search. Use `grep` for exact text "
            "and `read_file` for a file; `nih tools` says what is configured."
        ),
        summary=f"{accepted.tool}: not configured for this run",
    )


@asynccontextmanager
async def attach(
    server: ExternalServer,
    *,
    limits: ToolLimits | None = None,
) -> AsyncIterator[ExternalTools]:
    """Start one declared server and yield its tools.

    Args:
        server: the declaration to start.
        limits: result caps.

    Yields:
        The server's tools, bound to a live session.

    Raises:
        ServerUnavailableError: if the command is not there, the process dies,
            or the handshake does not complete. A server named in the
            configuration is part of what a role is allowed to do, so failing
            to reach it stops the run rather than quietly narrowing it.
    """
    _executable(server)
    starting = True
    try:
        async with (
            stdio_client(server.parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            starting = False
            yield ExternalTools(server, session, limits=limits)
    except BaseExceptionGroup as group:
        # Whatever failed - the handshake, or the caller's own work while the
        # session was open - comes back wrapped by two task groups. Unwrap it,
        # or the caller catches nothing it wrote a clause for.
        failure = unwrap_group(group)
        if starting and isinstance(failure, OSError | MCPError | ValidationError):
            raise ServerUnavailableError(server.name, failure) from group
        raise failure from None
    except (OSError, MCPError, ValidationError) as error:
        if starting:
            raise ServerUnavailableError(server.name, error) from error
        raise


def _executable(server: ExternalServer) -> None:
    """Check the command exists before anything is spawned.

    Raises:
        ServerUnavailableError: when the command cannot be found. This is the
            common failure and it deserves a message that names the command
            rather than a traceback from the process layer.
    """
    command = server.parameters.command
    if "/" in command:
        if Path(command).is_file():
            return
    elif shutil.which(command) is not None:
        return
    raise ServerUnavailableError(server.name, f"{command!r} is not an executable on this host")


@asynccontextmanager
async def connect_tools(
    primary: StdioServerParameters,
    servers: Sequence[ExternalServer] = (),
    *,
    limits: ToolLimits | None = None,
    timeout_seconds: float = 120.0,
    local: LocalToolProvider | None = None,
) -> AsyncIterator[CompositeToolProvider]:
    """Start the sandbox's tool server and every declared external one.

    Args:
        primary: how to start the harness's own tool server, normally the
            sandbox.
        servers: the declared external servers, in configuration order.
        limits: result caps, applied to every server alike.
        timeout_seconds: per-call timeout for the harness's own server; an
            external server carries its own.
        local: tools the harness serves itself, because the sandbox cannot
            reach what they need. Nothing is started for it; it is already
            running in this process.

    Yields:
        A provider over all of them, and the catalogue of what was attached.
    """
    async with AsyncExitStack() as stack:
        own = await stack.enter_async_context(
            connect_stdio(primary, limits=limits, timeout_seconds=timeout_seconds),
        )
        attached: dict[str, ExternalTools] = {}
        catalog: list[ExternalTool] = []
        for server in servers:
            tools = await stack.enter_async_context(attach(server, limits=limits))
            catalog.extend(await tools.discover())
            attached[server.name] = tools
        yield CompositeToolProvider(
            own,
            attached,
            ToolCatalog(external=tuple(catalog)),
            local,
        )

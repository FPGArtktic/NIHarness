# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools over the Model Context Protocol.

The harness is an MCP *client*; the servers run in the sandbox. This adapter is
the narrow bridge between the two, and it is deliberately suspicious of what
comes back:

* a server may advertise whatever it likes - only tools the harness knows and
  the role is allowed to use are ever forwarded to the model,
* arguments are the ones the domain already validated, never the model's raw
  proposal,
* results are truncated to the configured cap before they reach the window, and
  a transport failure becomes a red result rather than an exception that would
  take the step down with it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from niharness.domain.messages import ToolSchema
from niharness.domain.tools import (
    AcceptedCall,
    ToolLimits,
    ToolName,
    ToolResult,
    truncate_result,
)

TARGET_FIELDS: tuple[str, ...] = ("path", "selector", "target", "pattern", "query", "name")
"""Argument names that identify what a call acted on, in order of preference."""

ENVELOPE_KEYS: frozenset[str] = frozenset({"ok", "text"})
"""The harness's own tool servers answer with this envelope as structured content.

MCP's error channel masks an exception's detail, and the detail is what the model
has to read - which path escaped, what the gate printed. A server of this
project's own therefore says plainly whether the call worked. Any other server
is read the ordinary way, through `isError` and the content blocks.
"""

SUMMARY_LENGTH = 96
"""How much of a result's first line goes into the one-line window digest."""

_log = structlog.get_logger(__name__)


class McpToolProvider:
    """A :class:`niharness.ports.tool_provider.ToolProvider` over an MCP session."""

    def __init__(
        self,
        session: ClientSession,
        *,
        limits: ToolLimits | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        """Wrap an initialised MCP client session.

        Args:
            session: a session whose `initialize` has already completed.
            limits: result caps; the shipped defaults when omitted.
            timeout_seconds: how long a single tool call may take.
        """
        self._session = session
        self._limits = limits or ToolLimits()
        self._timeout = timeout_seconds

    async def descriptors(self, allowlist: frozenset[ToolName]) -> tuple[ToolSchema, ...]:
        """Return the schemas of the allowed tools the server actually offers.

        A tool the harness does not know, or that this role may not use, is not
        forwarded - a server cannot widen a role's permissions by advertising
        something extra.
        """
        listing = await self._session.list_tools()
        schemas: list[ToolSchema] = []
        refused: list[str] = []
        for tool in listing.tools:
            try:
                known = ToolName(tool.name)
            except ValueError:
                refused.append(tool.name)
                continue
            if known not in allowlist:
                refused.append(tool.name)
                continue
            schemas.append(
                ToolSchema(
                    name=known.value,
                    description=tool.description or known.value,
                    parameters=dict(tool.input_schema),
                ),
            )
        if refused:
            _log.info("tools.filtered", refused=sorted(refused), offered=len(schemas))
        return tuple(sorted(schemas, key=lambda schema: schema.name))

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Execute a validated call and return a truncated, structured result."""
        arguments: dict[str, Any] = accepted.args.model_dump(mode="json")
        try:
            raw = await self._session.call_tool(
                accepted.tool.value,
                arguments,
                read_timeout_seconds=self._timeout,
            )
        except MCPError as error:
            _log.warning("tool.transport_error", tool=accepted.tool.value, error=str(error))
            return ToolResult(
                call_id=accepted.call_id,
                tool=accepted.tool,
                ok=False,
                content=f"the tool server failed to run {accepted.tool.value}: {error}",
                summary=f"{accepted.tool.value}: transport error",
            )
        envelope = self._envelope(raw)
        ok, text = envelope if envelope is not None else (not raw.is_error, self._render(raw))
        content, truncated = truncate_result(text, self._limits)
        return ToolResult(
            call_id=accepted.call_id,
            tool=accepted.tool,
            ok=ok,
            content=content,
            truncated=truncated,
            summary=self._summary(accepted, text, arguments),
        )

    @staticmethod
    def _envelope(raw: types.CallToolResult) -> tuple[bool, str] | None:
        """Return this project's `(ok, text)` envelope, or None for a foreign server."""
        structured = raw.structured_content
        if not isinstance(structured, dict) or not set(structured) >= ENVELOPE_KEYS:
            return None
        ok, text = structured["ok"], structured["text"]
        if isinstance(ok, bool) and isinstance(text, str):
            return ok, text
        return None

    @staticmethod
    def _render(raw: types.CallToolResult) -> str:
        """Flatten a result's content blocks into text the window can carry."""
        parts: list[str] = []
        for block in raw.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(f"[{type(block).__name__} omitted: the window carries text]")
        if not parts and raw.structured_content is not None:
            return json.dumps(raw.structured_content, indent=2, sort_keys=True)
        return "\n".join(parts)

    @staticmethod
    def _summary(accepted: AcceptedCall, text: str, arguments: dict[str, Any]) -> str:
        """Return the one-line digest the window keeps once the body is compacted."""
        target = ""
        for field in TARGET_FIELDS:
            value = arguments.get(field)
            if isinstance(value, str) and value:
                target = value
                break
        where = f" {target}" if target else ""
        lines = text.count("\n") + 1 if text else 0
        head = text.strip().splitlines()[0][:SUMMARY_LENGTH] if text.strip() else ""
        detail = f": {lines} lines" if lines > 1 else (f": {head}" if head else "")
        return f"{accepted.tool.value}{where}{detail}"


@asynccontextmanager
async def connect_stdio(
    parameters: StdioServerParameters,
    *,
    limits: ToolLimits | None = None,
    timeout_seconds: float = 120.0,
) -> AsyncIterator[McpToolProvider]:
    """Start a tool server over stdio and yield a provider for it.

    The harness does not care how the server is launched: `parameters` normally
    names the sandbox (`podman run -i ...`), which is what keeps the tools
    inside a container with no network. Launching it any other way is the
    caller's decision and the caller's risk.

    Args:
        parameters: how to start the server process.
        limits: result caps.
        timeout_seconds: per-call timeout.

    Yields:
        A provider bound to the running session.
    """
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        _log.info("tools.connected", command=parameters.command)
        yield McpToolProvider(session, limits=limits, timeout_seconds=timeout_seconds)

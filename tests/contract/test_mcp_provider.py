# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MCP adapter, against a tool server running in this process.

No subprocess, no container, no network: the SDK's in-memory streams connect a
real client session to a real server, so the protocol is exercised while the
test stays hermetic. The cases that need a result the SDK's high-level server
will not produce - a transport failure, a non-text block - use a stub session
built from the SDK's own types.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio
import pytest
from mcp import ClientSession, types
from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from niharness.adapters.mcp import McpToolProvider
from niharness.domain.identity import CallId
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tools import (
    AcceptedCall,
    GrepArgs,
    ReadFileArgs,
    ToolLimits,
    ToolName,
)

IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist
REVIEWER = DEFAULT_ROLES[Role.REVIEWER].allowlist


@asynccontextmanager
async def provider_for(server: MCPServer, **kwargs: Any) -> AsyncIterator[McpToolProvider]:
    """Run `server` in this process and yield a provider connected to it."""
    inner = server._lowlevel_server  # noqa: SLF001 - the SDK's own in-process pattern
    async with AsyncExitStack() as stack:
        client, server_side = await stack.enter_async_context(
            create_client_server_memory_streams(),
        )
        read, write = client
        server_read, server_write = server_side
        group = await stack.enter_async_context(anyio.create_task_group())

        async def serve() -> None:
            await inner.run(server_read, server_write, inner.create_initialization_options())

        group.start_soon(serve)
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        yield McpToolProvider(session, **kwargs)
        group.cancel_scope.cancel()


def read_call(path: str = "src/a.py", call_id: str = "c1") -> AcceptedCall:
    return AcceptedCall(
        call_id=CallId(call_id),
        tool=ToolName.READ_FILE,
        args=ReadFileArgs(path=path),
    )


def toolbox(
    body: str = "line one\nline two\nline three",
) -> tuple[MCPServer, list[dict[str, Any]]]:
    """A server offering two allowed tools, one forbidden and one unknown."""
    server = MCPServer("fake-tools")
    seen: list[dict[str, Any]] = []

    @server.tool()
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a slice of one file."""
        seen.append({"tool": "read_file", "path": path, "start_line": start_line})
        if path == "missing.py":
            msg = "no such file"
            raise FileNotFoundError(msg)
        return body

    @server.tool()
    def grep(pattern: str, path: str = ".", glob: str = "", max_matches: int = 50) -> str:
        """Search the worktree."""
        seen.append(
            {"tool": "grep", "pattern": pattern, "path": path, "max_matches": max_matches},
        )
        return "src/a.py:3: TODO"

    @server.tool()
    def shell(command: str) -> str:
        """In no default allowlist."""
        seen.append({"tool": "shell", "command": command})
        return "should never run"

    @server.tool()
    def exfiltrate_secrets() -> str:
        """A tool the harness has never heard of."""
        return "nope"

    return server, seen


async def test_only_known_and_allowed_tools_reach_the_model() -> None:
    server, _ = toolbox()
    async with provider_for(server) as provider:
        schemas = await provider.descriptors(IMPLEMENTER)
    names = [schema.name for schema in schemas]
    assert names == ["grep", "read_file"], "sorted, and nothing the role may not use"
    assert "shell" not in names, "a server cannot widen a role's permissions"
    assert "exfiltrate_secrets" not in names, "an unknown tool is not forwarded"


async def test_the_servers_description_and_schema_are_passed_through() -> None:
    server, _ = toolbox()
    async with provider_for(server) as provider:
        schemas = {schema.name: schema for schema in await provider.descriptors(IMPLEMENTER)}
    assert schemas["read_file"].description.startswith("Read a slice")
    assert schemas["read_file"].parameters["type"] == "object"
    properties = schemas["read_file"].parameters["properties"]
    assert isinstance(properties, dict)
    assert "path" in properties


async def test_a_reviewer_is_never_offered_a_tool_that_changes_the_tree() -> None:
    server, _ = toolbox()
    async with provider_for(server) as provider:
        names = {schema.name for schema in await provider.descriptors(REVIEWER)}
    assert names == {"grep", "read_file"}


async def test_a_call_returns_text_and_a_one_line_summary() -> None:
    server, _ = toolbox()
    async with provider_for(server) as provider:
        result = await provider.call(read_call())
    assert result.ok
    assert result.content == "line one\nline two\nline three"
    assert not result.truncated
    assert result.summary == "read_file src/a.py: 3 lines"
    assert (result.tool, result.call_id) == (ToolName.READ_FILE, "c1")


async def test_the_validated_arguments_are_what_the_server_receives() -> None:
    server, seen = toolbox()
    async with provider_for(server) as provider:
        await provider.call(
            AcceptedCall(
                call_id=CallId("c2"),
                tool=ToolName.GREP,
                args=GrepArgs(pattern="TODO", path="src"),
            ),
        )
    assert seen == [{"tool": "grep", "pattern": "TODO", "path": "src", "max_matches": 50}]


async def test_a_failing_tool_is_a_red_result_not_an_exception() -> None:
    """A tool that raises comes back as a red result, with whatever the server reveals.

    How much detail a server puts in an error is the server's policy; the
    adapter's job is to carry it and to keep `ok` false.
    """
    server, _ = toolbox()
    async with provider_for(server) as provider:
        result = await provider.call(read_call(path="missing.py"))
    assert not result.ok
    assert "read_file" in result.content


async def test_a_long_result_is_truncated_before_it_reaches_the_window() -> None:
    server, _ = toolbox(body="x" * 5_000)
    async with provider_for(server, limits=ToolLimits(max_result_bytes=512)) as provider:
        result = await provider.call(read_call())
    assert result.truncated
    assert len(result.content.encode()) < 1_200
    assert "elided" in result.content


class StubSession:
    """A session that returns results the high-level server will not produce."""

    def __init__(self, result: types.CallToolResult | None = None) -> None:
        self.result = result
        self.timeouts: list[float | None] = []

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[])

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
    ) -> types.CallToolResult:
        self.timeouts.append(read_timeout_seconds)
        if self.result is None:
            raise MCPError(code=-32000, message="connection closed")
        return self.result


async def test_a_transport_failure_becomes_a_red_result() -> None:
    session = StubSession()
    provider = McpToolProvider(session, timeout_seconds=42.0)  # type: ignore[arg-type]
    result = await provider.call(read_call())
    assert not result.ok
    assert "connection closed" in result.content
    assert result.summary == "read_file: transport error"
    assert session.timeouts == [42.0], "the per-call timeout reaches the session"


async def test_non_text_content_is_named_rather_than_dropped_silently() -> None:
    session = StubSession(
        types.CallToolResult(
            content=[
                types.TextContent(type="text", text="before"),
                types.ImageContent(type="image", data="aGk=", mime_type="image/png"),
            ],
        ),
    )
    provider = McpToolProvider(session)  # type: ignore[arg-type]
    result = await provider.call(read_call())
    assert "before" in result.content
    assert "ImageContent omitted" in result.content


async def test_structured_output_is_rendered_when_there_is_no_text() -> None:
    session = StubSession(
        types.CallToolResult(content=[], structured_content={"files": 3, "insertions": 12}),
    )
    provider = McpToolProvider(session)  # type: ignore[arg-type]
    result = await provider.call(read_call())
    assert '"files": 3' in result.content


@pytest.mark.parametrize("allowlist", [frozenset(), DEFAULT_ROLES[Role.COMPACTOR].allowlist])
async def test_a_role_without_tools_is_offered_nothing(allowlist: frozenset[ToolName]) -> None:
    server, _ = toolbox()
    async with provider_for(server) as provider:
        assert await provider.descriptors(allowlist) == ()

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Declared MCP servers, against a real server running in this process.

The same in-memory streams as `test_mcp_provider.py`: a real client session
talks to a real server, so the protocol is exercised without a subprocess or a
container. What is under test is everything the harness adds around that - the
namespace, the import list, the routing, and the refusals.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.server import MCPServer
from mcp.shared.memory import create_client_server_memory_streams

from niharness.adapters.mcp import (
    CompositeToolProvider,
    ExternalServer,
    ExternalTools,
    ServerUnavailableError,
    ToolNotOfferedError,
    attach,
    unwrap_group,
)
from niharness.domain.identity import CallId
from niharness.domain.messages import ToolSchema
from niharness.domain.tools import (
    EMPTY_CATALOG,
    AcceptedCall,
    ExternalArgs,
    ExternalTool,
    RagQueryArgs,
    ReadFileArgs,
    ToolCatalog,
    ToolId,
    ToolLimits,
    ToolName,
    ToolResult,
)

DECLARED = ExternalServer(
    name="fpga",
    parameters=StdioServerParameters(command="/bin/true"),
    tools=frozenset({"read_report"}),
    roles=frozenset({"implementer"}),
)


def reports() -> tuple[MCPServer, list[dict[str, object]]]:
    """A server offering one imported tool and one the configuration ignores."""
    server = MCPServer("vendor-tools")
    seen: list[dict[str, object]] = []

    @server.tool()
    def read_report(design: str, stage: str = "synth") -> str:
        """Read a timing report."""
        seen.append({"tool": "read_report", "design": design, "stage": stage})
        return f"{design}: slack 0.42 ns"

    @server.tool()
    def program_device(bitstream: str) -> str:
        """Not imported by the configuration, so unreachable."""
        seen.append({"tool": "program_device", "bitstream": bitstream})
        return "programmed"

    return server, seen


@asynccontextmanager
async def tools_for(
    server: MCPServer,
    declared: ExternalServer = DECLARED,
    limits: ToolLimits | None = None,
) -> AsyncIterator[ExternalTools]:
    """Run `server` in this process and yield the harness's view of it."""
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
        yield ExternalTools(declared, session, limits=limits)
        group.cancel_scope.cancel()


def external_call(tool: ToolId, **arguments: object) -> AcceptedCall:
    return AcceptedCall(
        call_id=CallId("c1"),
        tool=tool,
        args=ExternalArgs.model_validate(dict(arguments)),
    )


# ------------------------------------------------------------------- discovery


async def test_only_the_imported_tools_are_offered() -> None:
    server, _ = reports()
    async with tools_for(server) as tools:
        imported = await tools.discover()
    assert [tool.id for tool in imported] == ["fpga__read_report"]
    assert imported[0].description.startswith("Read a timing report")
    assert imported[0].roles == frozenset({"implementer"})


async def test_the_servers_own_schema_is_what_the_model_is_told() -> None:
    server, _ = reports()
    async with tools_for(server) as tools:
        imported = await tools.discover()
    properties = imported[0].parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"design", "stage"}


async def test_a_declared_tool_the_server_does_not_offer_stops_the_run() -> None:
    server, _ = reports()
    declared = DECLARED.model_copy(update={"tools": frozenset({"read_report", "place"})})
    async with tools_for(server, declared) as tools:
        with pytest.raises(ToolNotOfferedError, match="does not offer place"):
            await tools.discover()


# ------------------------------------------------------------------- execution


async def test_a_call_reaches_the_server_under_its_own_name() -> None:
    server, seen = reports()
    async with tools_for(server) as tools:
        result = await tools.call(external_call("fpga__read_report", design="top"))
    assert seen == [{"tool": "read_report", "design": "top", "stage": "synth"}]
    assert result.ok
    assert result.tool == "fpga__read_report"
    assert "slack 0.42" in result.content
    # The digest line names the server's tool; "design" is not one of the
    # argument names the harness recognises as a target, so the first line of
    # the result stands in for one.
    assert result.summary == "fpga__read_report: top: slack 0.42 ns"


async def test_a_failing_tool_is_a_red_result_not_an_exception() -> None:
    server = MCPServer("vendor-tools")

    @server.tool()
    def read_report(design: str) -> str:
        """Always fails."""
        msg = "the project is not open"
        raise RuntimeError(msg)

    async with tools_for(server) as tools:
        result = await tools.call(external_call("fpga__read_report", design="top"))
    assert not result.ok


async def test_a_result_is_truncated_to_the_configured_cap() -> None:
    server = MCPServer("vendor-tools")

    @server.tool()
    def read_report(design: str) -> str:
        """Return more than the window will carry."""
        return "x" * 40_000

    async with tools_for(server, limits=ToolLimits(max_result_bytes=1_000)) as tools:
        result = await tools.call(external_call("fpga__read_report", design="top"))
    assert result.truncated
    assert len(result.content.encode("utf-8")) < 2_000


# --------------------------------------------------------------------- routing


class FakeSandbox:
    """The harness's own tool server, as far as the router can tell.

    It offers the built-in names and nothing else - not the namespaced ones,
    which belong to a declared server, and not `rag_query`, which the real
    tool server does not register because the container has no network.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(name=name, description=name, parameters={"type": "object"})
            for name in sorted(allowlist)
            if "__" not in name and name != ToolName.RAG_QUERY
        )

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        self.calls.append(accepted.tool)
        return ToolResult(call_id=accepted.call_id, tool=accepted.tool, ok=True, content="in")


async def test_a_builtin_call_goes_to_the_sandbox_and_a_namespaced_one_does_not() -> None:
    server, seen = reports()
    sandbox = FakeSandbox()
    async with tools_for(server) as tools:
        catalog = ToolCatalog(external=await tools.discover())
        router = CompositeToolProvider(sandbox, {"fpga": tools}, catalog)
        own = await router.call(
            AcceptedCall(
                call_id=CallId("c0"),
                tool=ToolName.READ_FILE,
                args=ReadFileArgs(path="src/a.py"),
            ),
        )
        external = await router.call(external_call("fpga__read_report", design="top"))
    assert own.content == "in"
    assert sandbox.calls == ["read_file"], "the sandbox never saw the external call"
    assert seen == [{"tool": "read_report", "design": "top", "stage": "synth"}]
    assert external.ok


class FakeLocal:
    """A tool the harness serves itself, as far as the router can tell."""

    def __init__(self, served: frozenset[ToolId]) -> None:
        self._served = served
        self.calls: list[str] = []

    @property
    def served(self) -> frozenset[ToolId]:
        return self._served

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(name=name, description=name, parameters={"type": "object"})
            for name in sorted(self._served & allowlist)
        )

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        self.calls.append(accepted.tool)
        return ToolResult(
            call_id=accepted.call_id,
            tool=accepted.tool,
            ok=True,
            content="from the harness",
        )


async def test_a_tool_the_harness_serves_itself_never_reaches_the_sandbox() -> None:
    """`rag_query` needs the vector store, and the sandbox has no network.

    The route has to be by name rather than by failure: a `rag_query` that
    went to the sandbox would come back as a tool the server does not offer,
    which reads to the model like the tool not existing.
    """
    sandbox = FakeSandbox()
    local = FakeLocal(frozenset({ToolId(ToolName.RAG_QUERY.value)}))
    router = CompositeToolProvider(sandbox, {}, EMPTY_CATALOG, local)
    answered = await router.call(
        AcceptedCall(
            call_id=CallId("c0"),
            tool=ToolName.RAG_QUERY,
            args=RagQueryArgs(query="what voltage"),
        ),
    )
    read = await router.call(
        AcceptedCall(
            call_id=CallId("c1"),
            tool=ToolName.READ_FILE,
            args=ReadFileArgs(path="src/a.py"),
        ),
    )
    assert answered.content == "from the harness"
    assert local.calls == ["rag_query"]
    assert sandbox.calls == ["read_file"], "the sandbox saw only what it holds"
    assert read.content == "in"


async def test_without_a_local_provider_its_tools_are_not_offered() -> None:
    """A store that is not running means no `rag_query`, not a broken one.

    The role's allowlist still names it - the allowlist says what a role may
    do, not what this host can do - so the tool list is where it disappears.
    """
    allowlist = frozenset({ToolId(ToolName.READ_FILE.value), ToolId(ToolName.RAG_QUERY.value)})
    router = CompositeToolProvider(FakeSandbox(), {}, EMPTY_CATALOG)
    offered = await router.descriptors(allowlist)
    with_store = await CompositeToolProvider(
        FakeSandbox(),
        {},
        EMPTY_CATALOG,
        FakeLocal(frozenset({ToolId(ToolName.RAG_QUERY.value)})),
    ).descriptors(allowlist)
    assert [schema.name for schema in offered] == ["read_file"]
    assert [schema.name for schema in with_store] == ["read_file", "rag_query"]


async def test_the_tool_list_is_the_sandbox_plus_what_the_role_was_granted() -> None:
    server, _ = reports()
    async with tools_for(server) as tools:
        catalog = ToolCatalog(external=await tools.discover())
        router = CompositeToolProvider(FakeSandbox(), {"fpga": tools}, catalog)
        allowed = await router.descriptors(
            frozenset({ToolName.READ_FILE, ToolName.GREP})
            | catalog.allowlist_for("implementer"),
        )
        withheld = await router.descriptors(frozenset({ToolName.READ_FILE}))
    assert [schema.name for schema in allowed] == ["grep", "read_file", "fpga__read_report"]
    assert [schema.name for schema in withheld] == ["read_file"]


async def test_an_external_tool_cannot_shadow_the_gate() -> None:
    # Whatever a server calls its tool, the harness addresses it under the
    # server's namespace, so `git_commit` stays the sandbox's.
    impostor = ExternalTool(server="fpga", name="git_commit", roles=frozenset({"implementer"}))
    assert impostor.id == "fpga__git_commit"
    assert impostor.id != ToolName.GIT_COMMIT


# ------------------------------------------------------------------- refusals


async def test_a_server_whose_command_is_not_there_is_reported_by_name() -> None:
    missing = DECLARED.model_copy(
        update={"parameters": StdioServerParameters(command="nih-no-such-mcp-server")},
    )
    with pytest.raises(ServerUnavailableError, match="nih-no-such-mcp-server"):
        async with attach(missing):
            pass  # pragma: no cover - attaching is what raises


# ------------------------------------------------------- over an actual pipe

SERVER_SCRIPT = """
from mcp.server import MCPServer

server = MCPServer("vendor-tools")


@server.tool()
def read_report(design: str) -> str:
    \"\"\"Read a timing report.\"\"\"
    return f"{design}: slack 0.42 ns"


server.run()
"""


def spawned(tmp_path: Path, tools: frozenset[str]) -> ExternalServer:
    """Declare a server that really is another process on the other end of a pipe."""
    script = tmp_path / "vendor_server.py"
    script.write_text(SERVER_SCRIPT, encoding="utf-8")
    return ExternalServer(
        name="fpga",
        parameters=StdioServerParameters(command=sys.executable, args=[str(script)]),
        tools=tools,
        roles=frozenset({"implementer"}),
    )


async def test_a_declared_tool_is_called_over_a_real_transport(tmp_path: Path) -> None:
    async with attach(spawned(tmp_path, frozenset({"read_report"}))) as tools:
        imported = await tools.discover()
        result = await tools.call(external_call("fpga__read_report", design="top"))
    assert [tool.id for tool in imported] == ["fpga__read_report"]
    assert result.ok
    assert "slack 0.42" in result.content


async def test_a_refusal_survives_the_transports_task_groups(tmp_path: Path) -> None:
    # The in-process cases raise straight into the caller. A real stdio session
    # runs inside two anyio task groups, each of which wraps whatever happened
    # inside it, so without unwrapping the caller's `except` clause never fires
    # and the operator gets a traceback instead of the reason.
    with pytest.raises(ToolNotOfferedError, match="does not offer place"):
        async with attach(spawned(tmp_path, frozenset({"read_report", "place"}))) as tools:
            await tools.discover()


def test_a_group_holding_one_failure_unwraps_to_it() -> None:
    inner = ToolNotOfferedError("fpga", ["place"], ["read_report"])
    wrapped = BaseExceptionGroup("transport", [BaseExceptionGroup("session", [inner])])
    assert unwrap_group(wrapped) is inner


def test_a_group_holding_two_failures_is_left_as_it_is() -> None:
    group = BaseExceptionGroup("both", [RuntimeError("a"), RuntimeError("b")])
    assert unwrap_group(group) is group

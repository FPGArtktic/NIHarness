# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MCP server the harness talks to.

Every tool answers with the same envelope - `{"ok": bool, "text": str}` - as
structured content. MCP's own error channel masks the detail of an exception,
and the detail is the point: a red gate's output is what the model has to read
to fix it. The harness's client understands this envelope and falls back to the
plain protocol for any other server.

Tool names and argument names are the ones in `niharness.domain.tools`, because
the harness validates a call against that schema *before* it is dispatched. A
server that renamed an argument would simply never be called.
"""

from __future__ import annotations

from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import structlog
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from niharness.domain.gate import GateKind, GateTier
from niharness.toolserver.lsp import LanguageServers
from niharness.toolserver.workspace import Workspace, WorkspaceError

SERVER_NAME = "niharness-tools"
"""How the server identifies itself in the MCP handshake."""

_log = structlog.get_logger(__name__)


class Outcome(BaseModel):
    """What every tool returns: whether it worked, and what the model should read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    text: str


async def guarded(call: Coroutine[Any, Any, str]) -> Outcome:
    """Await `call` and turn a refusal into a result the model can read.

    MCP's error channel masks an exception's detail, and the detail - which path
    escaped, which diff would not apply, what the gate printed - is the whole
    value of the answer.
    """
    try:
        return Outcome(ok=True, text=await call)
    except WorkspaceError as error:
        return Outcome(ok=False, text=str(error))


def build_server(workspace: Workspace, *, name: str = SERVER_NAME) -> MCPServer:
    """Register the worktree tools on a server bound to `workspace`.

    Args:
        workspace: the worktree the tools act on.
        name: the server's name in the handshake.

    Returns:
        A server ready to run over stdio, or over memory streams in a test.
    """
    server = MCPServer(name)

    _register_reading(server, workspace)
    _register_writing(server, workspace)
    _register_checks(server, workspace)

    _log.info("toolserver.ready", name=name, root=str(workspace.root))
    return server


def _register_reading(server: MCPServer, workspace: Workspace) -> None:
    """Register the tools that only read: files, searches and symbols."""

    @server.tool()
    async def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> Outcome:
        """Read a slice of one file inside the worktree, with line numbers."""
        return await guarded(workspace.read_file(path, start_line, end_line))

    @server.tool()
    async def list_tree(path: str = ".", depth: int = 2) -> Outcome:
        """List a directory inside the worktree, bounded in depth."""
        return await guarded(workspace.list_tree(path, depth))

    @server.tool()
    async def grep(
        pattern: str,
        path: str = ".",
        glob: str = "",
        max_matches: int = 50,
    ) -> Outcome:
        """Search the worktree for a regular expression."""
        return await guarded(workspace.grep(pattern, path, glob, max_matches))

    @server.tool()
    async def find_symbol(name: str, language: str = "") -> Outcome:
        """Find where a symbol is defined, by name, using the language servers.

        Prefer this over `grep` for a definition: a language server knows a
        definition from a string that merely matches, and the answer is a few
        lines instead of a page. `language` narrows the search when more than
        one language could answer.
        """
        return await guarded(workspace.find_symbol(name, language))

    @server.tool()
    async def find_references(name: str, max_results: int = 30, language: str = "") -> Outcome:
        """Find who uses a symbol, by name, with the line at each use.

        This is what to use before changing a signature. `grep` finds the name
        in comments and strings too; this finds the uses.
        """
        return await guarded(workspace.find_references(name, max_results, language))

    @server.tool()
    async def file_outline(path: str) -> Outcome:
        """List what is in a file - classes, functions, their lines - without reading it.

        A cheap way to decide whether a file is worth reading, and where in it
        to start.
        """
        return await guarded(workspace.file_outline(path))


def _register_writing(server: MCPServer, workspace: Workspace) -> None:
    """Register the tools that change the worktree, and the gate that lets one out."""

    @server.tool()
    async def apply_patch(diff: str) -> Outcome:
        """Apply a unified diff. There is no write_file: every change is a reviewable diff."""
        return await guarded(workspace.apply_patch(diff))

    @server.tool()
    async def format_code(path: str = ".") -> Outcome:
        """Run the project's formatters, in place.

        Use this instead of hand-writing a diff in the formatter's style: the
        gate checks formatting on every commit attempt, and guessing what a
        formatter wants is not work worth spending a budget on.
        """
        return await guarded(workspace.format_code(path))

    @server.tool()
    async def git_commit(message: str) -> Outcome:
        """Attempt a commit. This is the gate: format, lint, build and quick tests.

        A red gate means there is no commit, and the failing output comes back
        so it can be fixed. There is no flag that skips it.
        """
        return await guarded(workspace.git_commit(message))


def _register_checks(server: MCPServer, workspace: Workspace) -> None:
    """Register the tools that run the toolchain without changing anything."""

    @server.tool()
    async def run_lint(path: str = ".") -> Outcome:
        """Run the configured formatters and linters.

        The commands come from the gate matrix for the languages detected in the
        worktree. `path` does not narrow them yet, and the result says so rather
        than pretending it did.
        """
        note = "" if path == "." else f"(path {path!r} does not narrow the gate yet)\n"
        result = await workspace.run_gate(
            GateTier.QUICK,
            frozenset({GateKind.FORMAT, GateKind.LINT}),
        )
        return Outcome(ok=result.passed, text=note + workspace.render_gate(result))

    @server.tool()
    async def run_build(target: str = "") -> Outcome:
        """Run the configured build for the detected languages."""
        note = "" if not target else f"(target {target!r} does not narrow the gate yet)\n"
        result = await workspace.run_gate(GateTier.QUICK, frozenset({GateKind.BUILD}))
        return Outcome(ok=result.passed, text=note + workspace.render_gate(result))

    @server.tool()
    async def run_tests(selector: str = "") -> Outcome:
        """Run the configured test suites for the detected languages."""
        note = "" if not selector else f"(selector {selector!r} does not narrow the gate yet)\n"
        result = await workspace.run_gate(GateTier.QUICK, frozenset({GateKind.TEST}))
        return Outcome(ok=result.passed, text=note + workspace.render_gate(result))


def workspace_for(root: Path, *, servers: LanguageServers | None = None) -> Workspace:
    """Build the worktree the tools act on, with language servers when enabled."""
    return Workspace(root, servers=servers)


def server_for(root: Path, **kwargs: object) -> MCPServer:
    """Build a server for the worktree at `root`, without language servers."""
    return build_server(Workspace(root), **kwargs)  # type: ignore[arg-type]

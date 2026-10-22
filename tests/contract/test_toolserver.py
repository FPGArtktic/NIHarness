# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The harness talking to its own tool server, end to end, in one process.

A real MCP server over the SDK's memory streams, the real client adapter, and
the real domain validation in between. What is faked is the toolchain: gate
commands come from an injected runner, so these tests need no compiler and no
container - but git is real, because a commit that does not happen is the thing
being tested.
"""

import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from niharness.adapters.mcp import McpToolProvider
from niharness.domain.gate import Language
from niharness.domain.identity import CallId
from niharness.domain.language_servers import ServerCandidate
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tools import (
    AcceptedCall,
    ApplyPatchArgs,
    FileOutlineArgs,
    FindReferencesArgs,
    FindSymbolArgs,
    GitCommitArgs,
    GrepArgs,
    ListTreeArgs,
    ReadFileArgs,
    RunTestsArgs,
    ToolName,
    ToolResult,
)
from niharness.ports.sandbox import ProcessResult
from niharness.toolserver.lsp import LanguageServers
from niharness.toolserver.server import build_server
from niharness.toolserver.workspace import Workspace, subprocess_runner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (root / "src" / "a.py").write_text(
        "def add(x, y):\n    return x + y  # TODO: types\n", encoding="utf-8"
    )
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-m", "chore: initial commit\n\nSeed the worktree.")
    return root


def gate_runner(*, green: bool) -> object:
    """A runner that lets git through and answers gate commands green or red."""

    async def run(argv: Sequence[str], cwd: Path, timeout: int) -> ProcessResult:
        if argv[0] == "git":
            return await subprocess_runner(argv, cwd, timeout)
        if green:
            return ProcessResult(
                argv=tuple(argv), exit_code=0, stdout="ok", duration_seconds=0.1
            )
        return ProcessResult(
            argv=tuple(argv),
            exit_code=1,
            stdout="src/a.py:2: error: missing type annotation",
            duration_seconds=0.2,
        )

    return run


@asynccontextmanager
async def connected(
    worktree: Path,
    *,
    green: bool = True,
    servers: LanguageServers | None = None,
) -> AsyncIterator[McpToolProvider]:
    """Run the project's own tool server and yield the harness's client for it."""
    workspace = Workspace(
        worktree,
        runner=gate_runner(green=green),  # type: ignore[arg-type]
        servers=servers,
    )
    server = build_server(workspace)
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
        yield McpToolProvider(session)
        group.cancel_scope.cancel()


async def call(provider: McpToolProvider, tool: ToolName, args: object) -> ToolResult:
    return await provider.call(
        AcceptedCall(call_id=CallId("c1"), tool=tool, args=args),  # type: ignore[arg-type]
    )


async def test_the_server_offers_exactly_the_tools_the_role_may_use(worktree: Path) -> None:
    async with connected(worktree) as provider:
        names = {schema.name for schema in await provider.descriptors(IMPLEMENTER)}
    assert names == {
        "read_file",
        "list_tree",
        "grep",
        "find_symbol",
        "find_references",
        "file_outline",
        "apply_patch",
        "run_lint",
        "run_build",
        "run_tests",
        "git_commit",
    }


async def test_reading_a_file_returns_numbered_lines(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.READ_FILE, ReadFileArgs(path="src/a.py"))
    assert result.ok
    assert "1  def add(x, y):" in result.content


async def test_a_path_that_escapes_is_refused_by_the_server_too(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(
            provider, ToolName.READ_FILE, ReadFileArgs(path="src/../../secrets")
        )
    assert not result.ok
    assert "path_escape" in result.content


async def test_a_symlink_out_of_the_worktree_is_refused(worktree: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the model\n", encoding="utf-8")
    (worktree / "escape.txt").symlink_to(secret)
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.READ_FILE, ReadFileArgs(path="escape.txt"))
    assert not result.ok
    assert "outside the worktree" in result.content


async def test_grep_finds_the_line_and_names_the_file(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.GREP, GrepArgs(pattern="TODO"))
    assert result.ok
    assert "src/a.py:2:" in result.content


async def test_listing_shows_the_tree_without_the_git_directory(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.LIST_TREE, ListTreeArgs(path="."))
    assert result.ok
    assert "src/a.py" in result.content
    assert ".git" not in result.content


async def test_a_patch_is_applied_to_the_worktree(worktree: Path) -> None:
    diff = (
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def add(x, y):\n"
        "+def add(x: int, y: int) -> int:\n"
        "     return x + y  # TODO: types\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert result.ok, result.content
    assert "def add(x: int" in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_patch_that_does_not_apply_is_a_refusal_with_the_reason(worktree: Path) -> None:
    diff = "--- a/src/missing.py\n+++ b/src/missing.py\n@@ -1 +1 @@\n-old\n+new\n"
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert not result.ok
    assert "does not apply" in result.content


async def test_a_red_gate_means_no_commit(worktree: Path) -> None:
    (worktree / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")
    before = git(worktree, "rev-parse", "HEAD").strip()
    async with connected(worktree, green=False) as provider:
        result = await call(
            provider,
            ToolName.GIT_COMMIT,
            GitCommitArgs(message="feat: add b\n\nBecause the task asked for it."),
        )
    assert not result.ok
    assert "the gate is red" in result.content
    assert "missing type annotation" in result.content, "the failing output comes back"
    assert git(worktree, "rev-parse", "HEAD").strip() == before, "nothing was committed"


async def test_a_green_gate_commits(worktree: Path) -> None:
    (worktree / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")
    before = git(worktree, "rev-parse", "HEAD").strip()
    async with connected(worktree, green=True) as provider:
        result = await call(
            provider,
            ToolName.GIT_COMMIT,
            GitCommitArgs(message="feat: add b\n\nBecause the task asked for it."),
        )
    assert result.ok, result.content
    assert "committed" in result.content
    assert git(worktree, "rev-parse", "HEAD").strip() != before
    assert "feat: add b" in git(worktree, "log", "-1", "--pretty=%s")


async def test_a_commit_message_without_a_body_is_refused(worktree: Path) -> None:
    (worktree / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.GIT_COMMIT, GitCommitArgs(message="feat: add b"))
    assert not result.ok
    assert "body" in result.content


async def test_committing_a_clean_worktree_is_refused(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.GIT_COMMIT,
            GitCommitArgs(message="feat: nothing\n\nThere is nothing to do."),
        )
    assert not result.ok
    assert "nothing to commit" in result.content


async def test_running_tests_reports_the_gate(worktree: Path) -> None:
    async with connected(worktree, green=False) as provider:
        result = await call(provider, ToolName.RUN_TESTS, RunTestsArgs(selector="test_add"))
    assert not result.ok
    assert "does not narrow the gate yet" in result.content
    assert "RED" in result.content


# -------------------------------------------------------------- symbol tools

FAKE_SERVER = Path(__file__).with_name("fake_language_server.py")


def language_session(worktree: Path) -> LanguageServers:
    """A session whose Python server is the fake one from `test_lsp.py`."""
    return LanguageServers(
        worktree,
        overrides={
            Language.PYTHON: ServerCandidate(
                command=sys.executable,
                args=(
                    str(FAKE_SERVER),
                    "--root",
                    str(worktree),
                    "--record",
                    str(worktree / "record.jsonl"),
                ),
            ),
        },
        request_timeout=10.0,
        start_timeout=10.0,
    )


APP = (
    '"""The module under test."""\n\n\n'
    "def build(name):\n    return name\n\n\n"
    'def caller():\n    return build("x")\n'
)
"""`build` on line 4 and used on line 9, which is what the fake reports."""


async def test_a_symbol_is_found_through_the_protocol(worktree: Path) -> None:
    # What is under test is the whole path: the harness's validation, the MCP
    # call, the tool server, the language server, and the answer coming back
    # through the envelope.
    (worktree / "src" / "app.py").write_text(APP, encoding="utf-8")
    session = language_session(worktree)
    try:
        async with connected(worktree, servers=session) as provider:
            result = await call(provider, ToolName.FIND_SYMBOL, FindSymbolArgs(name="build"))
    finally:
        await session.aclose()
    assert result.ok
    assert "src/app.py:4  function build" in result.content
    assert "def build(name) -> str" in result.content


async def test_references_come_back_through_the_protocol(worktree: Path) -> None:
    (worktree / "src" / "app.py").write_text(APP, encoding="utf-8")
    session = language_session(worktree)
    try:
        async with connected(worktree, servers=session) as provider:
            result = await call(
                provider,
                ToolName.FIND_REFERENCES,
                FindReferencesArgs(name="build", max_results=5),
            )
    finally:
        await session.aclose()
    assert result.ok
    assert "is defined at src/app.py:4 and used:" in result.content
    assert 'src/app.py:9  return build("x")' in result.content


async def test_an_outline_comes_back_through_the_protocol(worktree: Path) -> None:
    (worktree / "src" / "app.py").write_text(APP, encoding="utf-8")
    session = language_session(worktree)
    try:
        async with connected(worktree, servers=session) as provider:
            result = await call(
                provider,
                ToolName.FILE_OUTLINE,
                FileOutlineArgs(path="src/app.py"),
            )
    finally:
        await session.aclose()
    assert result.ok
    assert "function build" in result.content
    assert "function caller" in result.content


async def test_a_symbol_tool_without_language_servers_says_so(worktree: Path) -> None:
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.FIND_SYMBOL, FindSymbolArgs(name="add"))
    assert not result.ok
    assert "language servers are not enabled" in result.content
    assert "grep" in result.content, "a refusal that names the alternative"


async def test_an_outline_outside_the_worktree_is_refused(worktree: Path) -> None:
    session = language_session(worktree)
    try:
        async with connected(worktree, servers=session) as provider:
            result = await call(
                provider,
                ToolName.FILE_OUTLINE,
                FileOutlineArgs(path="../../etc/passwd"),
            )
    finally:
        await session.aclose()
    assert not result.ok
    assert "path_escape" in result.content or "not a file" in result.content


# ------------------------------------------- the diffs a model actually writes


async def test_a_diff_without_the_a_and_b_prefixes_still_applies(worktree: Path) -> None:
    # A model writes the path it was shown. `git apply` strips one component by
    # default, so the path it was shown is the one that does not resolve.
    diff = (
        "--- src/a.py\n+++ src/a.py\n@@ -1,2 +1,3 @@\n"
        " def add(x, y):\n     return x + y  # TODO: types\n+VALUE = 1\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert result.ok, result.content
    assert "VALUE = 1" in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_hunk_header_counted_by_eye_still_applies(worktree: Path) -> None:
    diff = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,9 +1,9 @@\n"
        " def add(x, y):\n     return x + y  # TODO: types\n+VALUE = 2\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert result.ok, result.content
    assert "VALUE = 2" in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_stray_line_is_refused_with_the_line_and_the_rule(worktree: Path) -> None:
    # What a live 4B produced on the first run: text after the hunk, with no
    # prefix. git calls the patch corrupt and names a line number, which on its
    # own tells a model nothing.
    diff = (
        "--- src/a.py\n+++ src/a.py\n@@ -1,2 +1,4 @@\n"
        " def add(x, y):\n     return x + y  # TODO: types\n\nVALUE = 3\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert not result.ok
    assert "VALUE = 3" in result.content, "the offending line is quoted"
    assert "must start with a space" in result.content, "and the rule it breaks"
    assert "VALUE = 3" not in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_diff_against_content_that_is_not_there_is_still_refused(
    worktree: Path,
) -> None:
    # The tolerances are clerical only: a diff whose context does not match the
    # file is a wrong diff, and no option set makes it right.
    diff = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,3 @@\n"
        " def subtract(x, y):\n     return x - y\n+VALUE = 4\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert not result.ok
    assert "VALUE = 4" not in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_new_file_a_model_announced_wrongly_still_lands(worktree: Path) -> None:
    # What the live 4B wrote on its second run: a creation with the real path
    # on the `---` line, where git insists on /dev/null. The repair has to put
    # the file where the diff says, not one directory above it.
    diff = (
        "--- src/greeter/greet.py\n"
        "+++ src/greeter/greet.py\n"
        "@@ -0,0 +1,3 @@\n"
        '+"""Greeting."""\n'
        "+\n"
        "+VALUE = 5\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert result.ok, result.content
    assert "did not exist" in result.content, "the model is told what it got wrong"
    assert (worktree / "src" / "greeter" / "greet.py").is_file()
    assert not (worktree / "greeter").exists(), "not one directory too high"


async def test_a_patch_that_is_already_applied_is_not_a_failure(worktree: Path) -> None:
    # A live 9B re-sent the same patch for a file it had already written,
    # twice, until the loop detectors stopped the step. "does not apply" was
    # true and useless; what it needed to hear is that the change is in.
    diff = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,3 @@\n"
        " def add(x, y):\n     return x + y  # TODO: types\n+VALUE = 6\n"
    )
    async with connected(worktree) as provider:
        first = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
        again = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert first.ok
    assert again.ok, "the worktree is in the state the model asked for"
    assert "already applied" in again.content
    body = (worktree / "src" / "a.py").read_text(encoding="utf-8")
    assert body.count("VALUE = 6") == 1, "and it was not applied twice"


# The diff a live Bonsai 2 9B sent three times, which ended the run: a
# malformed `diff --git` line, a nonsense `index` line, `--- a/dev/null`
# instead of `--- /dev/null`, a hunk header counting one line too many, and no
# newline on the last line. Every one of those is clerical, and the change it
# describes is exactly right.
LIVE_9B_DIFF = (
    "diff --git a/src/greeter/greet.py\n"
    "index /dev/null b/src/greeter/greet.py\n"
    "new file mode 100644\n"
    "--- a/dev/null\n"
    "+++ b/src/greeter/greet.py\n"
    "@@ -0,0 +1,6 @@\n"
    '+"""A greeter helper."""\n'
    "+\n"
    "+def greet(name: str) -> str:\n"
    "+    return 'hi'\n"
    "+"
)


async def test_the_diff_that_ended_a_live_run_now_applies(worktree: Path) -> None:
    (worktree / "src" / "greeter").mkdir(parents=True)
    (worktree / "src" / "greeter" / "__init__.py").write_text('"""G."""\n', encoding="utf-8")
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=LIVE_9B_DIFF))
    assert result.ok, result.content
    assert "no newline" in result.content, "the model is told about the missing newline"
    assert "did not exist" in result.content, "and about /dev/null"
    written = (worktree / "src" / "greeter" / "greet.py").read_text(encoding="utf-8")
    assert "def greet(name: str) -> str:" in written

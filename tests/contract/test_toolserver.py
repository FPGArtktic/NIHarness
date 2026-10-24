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
    AddDependencyArgs,
    ApplyPatchArgs,
    FileOutlineArgs,
    FindReferencesArgs,
    FindSymbolArgs,
    FormatCodeArgs,
    GitCommitArgs,
    GrepArgs,
    ListTreeArgs,
    ReadFileArgs,
    ReplaceInFileArgs,
    RunLintArgs,
    RunTestsArgs,
    ToolName,
    ToolResult,
)
from niharness.ports.sandbox import ProcessResult
from niharness.toolserver.lsp import LanguageServers
from niharness.toolserver.server import build_server
from niharness.toolserver.workspace import Workspace, WorkspaceError, subprocess_runner

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
    runner: object | None = None,
) -> AsyncIterator[McpToolProvider]:
    """Run the project's own tool server and yield the harness's client for it."""
    workspace = Workspace(
        worktree,
        runner=runner or gate_runner(green=green),  # type: ignore[arg-type]
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


async def test_the_sandbox_does_not_offer_rag_query(worktree: Path) -> None:
    """Retrieval is served by the harness, because the container has no network.

    The tool is in every role's allowlist, so the only thing keeping it out of
    the sandbox is that the server never registers it. If that changed, a
    `rag_query` would be dispatched into a container that cannot reach the
    vector store, and the model would be told the store is down.
    """
    async with connected(worktree) as tools:
        offered = await tools.descriptors(IMPLEMENTER)
    assert ToolName.RAG_QUERY in IMPLEMENTER, "the role may call it"
    assert ToolName.RAG_QUERY.value not in {schema.name for schema in offered}
    assert {"read_file", "apply_patch", "git_commit"} <= {schema.name for schema in offered}


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
        "replace_in_file",
        "apply_patch",
        "format_code",
        "run_lint",
        "run_build",
        "run_tests",
        "git_commit",
        "add_dependency",
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


async def test_a_worktree_with_no_gate_says_so_instead_of_saying_red(
    tmp_path: Path,
) -> None:
    """A gate that ran nothing is a wall with no door, if it is called red.

    Observed on a live run against a project with `.py` files and no
    `pyproject.toml`: told "RED" with no failing command, the model ran the
    three checks again and the detectors stopped the step. The commit is
    still refused - a gate that verified nothing cannot authorise one - but
    the message has to name a cause that exists.
    """
    root = tmp_path / "plain"
    (root / "src").mkdir(parents=True)
    (root / "src" / "notes.txt").write_text("no language here\n", encoding="utf-8")
    git(root, "init", "-q", ".")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")

    async with connected(root) as provider:
        checked = await call(provider, ToolName.RUN_LINT, RunLintArgs(path="."))
        (root / "src" / "notes.txt").write_text("changed\n", encoding="utf-8")
        committed = await call(
            provider,
            ToolName.GIT_COMMIT,
            GitCommitArgs(message="feat(x): a change\n\nbody"),
        )
    assert checked.ok, "asking an unconfigured gate is not itself a failure"
    assert "nothing to run" in checked.content
    assert "RED" not in checked.content
    assert "marker file" in checked.content, "name the fix that exists"
    assert "NOTES.md" in checked.content, "and what to do when it is not yours to make"
    assert not committed.ok, "a gate that verified nothing cannot authorise a commit"
    assert "the code is not the reason" in committed.content


async def test_replace_in_file_writes_the_change_and_shows_the_diff_it_made(
    worktree: Path,
) -> None:
    """The point of the tool: the model says what, the harness writes the diff.

    What reaches the reviewer and the commit is still a diff - it is just not
    one the model had to type, so it cannot arrive malformed.
    """
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(
                path="src/a.py",
                find="    return x + y  # TODO: types",
                replace="    return x + y",
            ),
        )
    assert result.ok, result.content
    assert "replaced one passage in src/a.py" in result.content
    assert "@@" in result.content, "the diff the harness produced comes back"
    assert "-    return x + y  # TODO: types" in result.content
    body = (worktree / "src" / "a.py").read_text(encoding="utf-8")
    assert body == "def add(x, y):\n    return x + y\n"


async def test_replace_in_file_creates_a_file_with_an_empty_find(worktree: Path) -> None:
    """Creation was the one change this tool could not make.

    That left it to a hand-written creating diff, which is the format a small
    model fails most: on the 14-task L1 suite a 4B failed the creation task and
    nothing else about writing.
    """
    body = '"""A gauge."""\n\n\ndef reading() -> int:\n    return 0\n'
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(path="src/pkg/gauge.py", find="", replace=body),
        )
    assert result.ok, result.content
    assert "created src/pkg/gauge.py, 5 line(s)" in result.content
    assert "def reading" in result.content, "the creating diff comes back"
    landed = worktree / "src" / "pkg" / "gauge.py"
    assert landed.read_text(encoding="utf-8") == body
    assert landed.parent.is_dir(), "a missing directory is created, inside the worktree"


async def test_an_empty_find_against_a_file_that_exists_is_refused(worktree: Path) -> None:
    """It does not say what to change, and overwriting would lose the bound."""
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(path="src/a.py", find="", replace="gone"),
        )
    assert not result.ok
    assert "exists, so an empty `find`" in result.content
    assert "def add" in (worktree / "src" / "a.py").read_text(encoding="utf-8")


async def test_a_passage_sought_in_a_file_that_is_not_there_says_how_to_create_it(
    worktree: Path,
) -> None:
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(path="src/absent.py", find="x", replace="y"),
        )
    assert not result.ok
    assert "leave `find` empty" in result.content


async def test_replace_in_file_refuses_a_passage_that_is_not_unique(worktree: Path) -> None:
    """Two matches do not say which one, and guessing would be a silent wrong edit."""
    (worktree / "src" / "dup.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(path="src/dup.py", find="= 1", replace="= 2"),
        )
    assert not result.ok
    assert "occurs 2 times" in result.content
    assert (worktree / "src" / "dup.py").read_text(encoding="utf-8") == "a = 1\nb = 1\n"


async def test_replace_in_file_quotes_back_a_passage_whose_indentation_is_wrong(
    worktree: Path,
) -> None:
    """The commonest near miss, and the one with an exact answer available."""
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(
                path="src/a.py",
                find="def add(x, y):\nreturn x + y  # TODO: types",
                replace="x",
            ),
        )
    assert not result.ok
    assert "differs only in whitespace" in result.content
    assert "    return x + y  # TODO: types" in result.content


@pytest.mark.parametrize("path", ["../escaped.py", "/etc/hostname", "src/../../out.py"])
async def test_replace_in_file_is_confined_by_the_server_too(worktree: Path, path: str) -> None:
    """A write tool that is not a diff still cannot reach outside.

    The domain refuses these before dispatch, so this is the second layer:
    the argument model itself accepts the string, and the server resolves
    every path against the worktree root - which is what catches a symlink.
    """
    async with connected(worktree) as provider:
        result = await call(
            provider,
            ToolName.REPLACE_IN_FILE,
            ReplaceInFileArgs(path=path, find="", replace="x = 1\n"),
        )
    assert not result.ok
    assert not (worktree.parent / "escaped.py").exists()
    assert not (worktree.parent / "out.py").exists()


async def test_a_diff_written_from_read_files_numbered_output_still_lands(
    worktree: Path,
) -> None:
    # What the live 4B wrote after reading the file: `read_file` numbers its
    # answer so it can be read, and the model carried the numbers into the
    # context, where they are not in the file. Every line then mismatches and
    # git blames a hunk header. The harness put the numbers there, so it takes
    # them off - and says so.
    diff = (
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        "      1  def add(x, y):\n"
        "      2      return x + y  # TODO: types\n"
        "+VALUE = 7\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert result.ok, result.content
    assert "line numbers" in result.content, "the model is told what it got wrong"
    body = (worktree / "src" / "a.py").read_text(encoding="utf-8")
    assert "VALUE = 7" in body
    assert "1  def add" not in body, "the margin is not written into the file"


async def test_a_refused_diff_against_an_existing_file_names_the_better_tool(
    worktree: Path,
) -> None:
    """A model that has just been refused is reading this, not the prompt.

    Across live runs the same 4B sometimes reached for `replace_in_file` after
    a refusal and sometimes re-sent the diff until the detectors stopped the
    step. The message is where that variance can be removed.
    """
    wrong_context = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,3 @@\n"
        " def subtract(x, y):\n     return x - y\n+VALUE = 9\n"
    )
    creating = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+VALUE = 9\n"
    async with connected(worktree) as provider:
        refused = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=wrong_context))
        made = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=creating))
    assert not refused.ok
    assert "replace_in_file" in refused.content
    assert "copied out of the file" in refused.content, "say how to call it"
    assert made.ok, "a creation is unaffected"
    assert "replace_in_file" not in made.content, (
        "creating a file is still apply_patch's job, so do not send it elsewhere"
    )


async def test_a_diff_of_nothing_but_context_is_refused_rather_than_applied(
    worktree: Path,
) -> None:
    # Valid, applies cleanly against matching context, and changes nothing.
    # Reporting success here is the worst outcome available: the model goes on
    # to the gate believing it wrote the change, and the file is untouched.
    diff = (
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def add(x, y):\n"
        "     return x + y  # TODO: types\n"
        " VALUE = 8\n"
    )
    async with connected(worktree) as provider:
        result = await call(provider, ToolName.APPLY_PATCH, ApplyPatchArgs(diff=diff))
    assert not result.ok
    assert "changes nothing" in result.content
    assert "starts with '+'" in result.content, "and what to write instead"
    assert "VALUE = 8" not in (worktree / "src" / "a.py").read_text(encoding="utf-8")


def dependency_runner(*available: str) -> object:
    """A runner where only `available` is installed.

    The gate runner answers every non-git command green, which makes an
    availability probe always say yes - so these tests need one that can say
    no. git stays real, because `NOTES.md` lands in a worktree.
    """

    async def run(argv: Sequence[str], cwd: Path, timeout: int) -> ProcessResult:
        if argv[0] == "git":
            return await subprocess_runner(argv, cwd, timeout)
        asked = next((name for name in available if name in " ".join(argv)), "")
        if asked:
            return ProcessResult(
                argv=tuple(argv), exit_code=0, stdout=f"{asked}-1.0", duration_seconds=0.0
            )
        return ProcessResult(
            argv=tuple(argv), exit_code=1, stdout="not found", duration_seconds=0.0
        )

    return run


async def test_asking_for_a_dependency_records_it_and_installs_nothing(
    worktree: Path,
) -> None:
    """The documented behaviour, which had no implementation at all.

    `add_dependency` was in the domain and in two roles' allowlists, and the
    tool server never registered it - so a model that called it got "the tool
    server failed to run add_dependency", blaming the server for a tool that
    did not exist. The same hole `rag_query` had, left behind for the other
    name.
    """
    async with connected(worktree, runner=dependency_runner()) as provider:
        result = await call(
            provider,
            ToolName.ADD_DEPENDENCY,
            AddDependencyArgs(name="requests", version=">=2.32", ecosystem="python"),
        )
    assert result.ok, "what it asked for cannot happen, and the asking is not a failure"
    assert "nothing was installed" in result.content
    assert "NOTES.md" in result.content
    notes = (worktree / "NOTES.md").read_text(encoding="utf-8")
    assert "requests >=2.32" in notes
    assert "python" in notes


async def test_asking_twice_does_not_write_the_request_twice(worktree: Path) -> None:
    """A model that repeats itself should not grow the file."""
    async with connected(worktree, runner=dependency_runner()) as provider:
        first = await call(
            provider,
            ToolName.ADD_DEPENDENCY,
            AddDependencyArgs(name="requests", ecosystem="python"),
        )
        second = await call(
            provider,
            ToolName.ADD_DEPENDENCY,
            AddDependencyArgs(name="requests", ecosystem="python"),
        )
    assert first.ok
    assert second.ok
    assert "already in NOTES.md" in second.content
    notes = (worktree / "NOTES.md").read_text(encoding="utf-8")
    assert notes.count("requests") == 1


async def test_a_dependency_that_is_already_here_is_not_a_request(worktree: Path) -> None:
    """Asking for what is installed should say so, not file a note about it."""
    async with connected(worktree, runner=dependency_runner("pydantic")) as provider:
        result = await call(
            provider,
            ToolName.ADD_DEPENDENCY,
            AddDependencyArgs(name="pydantic", ecosystem="python"),
        )
    assert result.ok
    assert "already available" in result.content
    assert not (worktree / "NOTES.md").exists(), "nothing to record"


async def test_an_unknown_ecosystem_is_refused_with_the_ones_that_exist(
    worktree: Path,
) -> None:
    async with connected(worktree, runner=dependency_runner()) as provider:
        result = await call(
            provider,
            ToolName.ADD_DEPENDENCY,
            AddDependencyArgs(name="libfoo", ecosystem="python"),
        )
    assert result.ok, "python is known; this is the control for the next assertion"
    outcome = await guarded_ecosystem(worktree, "haskell")
    assert "unknown ecosystem" in outcome
    assert "python, rust, system" in outcome


async def guarded_ecosystem(worktree: Path, ecosystem: str) -> str:
    """Call the workspace directly: the args model forbids an unknown ecosystem."""
    workspace = Workspace(worktree, runner=dependency_runner())  # type: ignore[arg-type]
    try:
        return await workspace.add_dependency("libfoo", ecosystem=ecosystem)
    except WorkspaceError as error:
        return str(error)


async def test_a_patch_that_is_already_applied_is_not_a_failure(worktree: Path) -> None:
    # A live 9B re-sent the same patch for a file it had already written,
    # twice, until the loop detectors stopped the step. "does not apply" was
    # true and useless; what it needed to hear is that the change is in.
    diff = (
        "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,3 @@\n"
        " def add(x, y):\n     return x + y  # TODO: types\n+VALUE = 6\n"
    )
    async with connected(worktree, runner=dependency_runner()) as provider:
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


# ------------------------------------------------------ style is not a guess


async def test_the_formatter_runs_the_writing_form_of_the_gate_command(
    worktree: Path,
) -> None:
    # A live run was refused a correct, typed, documented function for one
    # missing blank line, and then spent its whole budget failing to guess what
    # `ruff format` wanted. What the harness owes the model is the formatter in
    # its *writing* form - `ruff format .`, not `ruff format --check .` - and
    # that is what this asserts. The toolchain itself stays faked here.
    seen: list[tuple[str, ...]] = []

    async def recording(argv: Sequence[str], cwd: Path, timeout: int) -> ProcessResult:
        if argv[0] == "git":
            return await subprocess_runner(argv, cwd, timeout)
        seen.append(tuple(argv))
        return ProcessResult(
            argv=tuple(argv), exit_code=0, stdout="1 file reformatted", duration_seconds=0.1
        )

    async with connected(worktree, runner=recording) as provider:
        result = await call(provider, ToolName.FORMAT_CODE, FormatCodeArgs())
    assert result.ok, result.content
    assert ("ruff", "format", ".") in seen, seen
    assert ("ruff", "format", "--check", ".") not in seen, (
        "that is the gate's form, not this one"
    )
    assert "1 file reformatted" in result.content, (
        "the formatter's own report reaches the model"
    )


async def test_the_formatter_says_so_when_a_project_has_none(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "doc").mkdir(parents=True)
    (root / "doc" / "notes.txt").write_text("nothing to format\n", encoding="utf-8")
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "-c", "commit.gpgsign=false", "commit", "-m", "chore: seed\n\nA body.")
    async with connected(root) as provider:
        result = await call(provider, ToolName.FORMAT_CODE, FormatCodeArgs())
    assert not result.ok
    assert "no formatter is configured" in result.content
    assert "the style the gate expects" in result.content

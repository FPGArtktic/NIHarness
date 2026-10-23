# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The language-server client and session, against a real process.

`fake_language_server.py` is started for real, over real pipes, and it asks the
client a question during startup the way pyright does - so these tests cover
the framing, the request correlation and the client's duty to answer the
server, none of which an in-process double would exercise.
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from niharness.domain.gate import Language
from niharness.domain.language_servers import ServerCandidate
from niharness.toolserver.lsp import LanguageServers, LspClient, LspTimeoutError
from niharness.toolserver.lsp.session import _hover_text

FIXTURE = '''"""The module under test."""


def build(name):
    return name


def caller():
    return build("x")
'''
"""`build` is defined on line 4 and used on line 9; the fake reports those."""

SERVER = Path(__file__).with_name("fake_language_server.py")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """A worktree with one Python file the fake server answers about."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(FIXTURE, encoding="utf-8")
    return tmp_path


def record_path(worktree: Path) -> Path:
    """Where the fake server writes what it received."""
    return worktree / "record.jsonl"


def received(worktree: Path) -> list[dict[str, object]]:
    """Return what the fake server recorded, in order."""
    path = record_path(worktree)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def servers(worktree: Path, *extra: str, request_timeout: float = 10.0) -> LanguageServers:
    """A session whose Python server is the fake one."""
    return LanguageServers(
        worktree,
        overrides={
            Language.PYTHON: ServerCandidate(
                command=sys.executable,
                args=(
                    str(SERVER),
                    "--root",
                    str(worktree),
                    "--record",
                    str(record_path(worktree)),
                    *extra,
                ),
            ),
        },
        request_timeout=request_timeout,
        start_timeout=10.0,
    )


# ------------------------------------------------------------------- handshake


async def test_the_handshake_answers_the_question_the_server_asks(worktree: Path) -> None:
    session = servers(worktree)
    try:
        client = await session.client_for(Language.PYTHON)
        assert client is not None
        assert client.running
        await session.find_symbol("build", [Language.PYTHON])
    finally:
        await session.aclose()
    answered = [entry for entry in received(worktree) if "answered" in entry]
    assert answered, "a server that is never answered waits for ever"
    assert answered[0]["result"] == [{}], "no settings is still an answer"


async def test_a_server_that_is_not_installed_is_reported_not_guessed(worktree: Path) -> None:
    session = LanguageServers(worktree, which=lambda command: None)
    try:
        assert await session.client_for(Language.PYTHON) is None
        answer = await session.find_symbol("build", [Language.PYTHON])
    finally:
        await session.aclose()
    assert "no language server is installed" in answer
    assert "grep still works" in answer


async def test_a_server_that_dies_is_not_started_again(worktree: Path) -> None:
    session = servers(worktree, "--die", request_timeout=2.0)
    try:
        assert await session.client_for(Language.PYTHON) is None
        assert await session.client_for(Language.PYTHON) is None
    finally:
        await session.aclose()


async def test_a_request_that_is_never_answered_times_out(worktree: Path) -> None:
    client = LspClient(
        sys.executable,
        (
            str(SERVER),
            "--root",
            str(worktree),
            "--record",
            str(record_path(worktree)),
            "--hang",
        ),
        worktree,
        request_timeout=0.5,
        start_timeout=10.0,
    )
    await client.start()
    try:
        with pytest.raises(LspTimeoutError, match="did not answer"):
            await client.request("workspace/symbol", {"query": "build"})
    finally:
        await client.stop()


# --------------------------------------------------------------------- answers


async def test_a_symbol_is_found_by_name_with_its_signature(worktree: Path) -> None:
    session = servers(worktree)
    try:
        answer = await session.find_symbol("build", [Language.PYTHON])
    finally:
        await session.aclose()
    assert "src/app.py:4  function build (in app)" in answer
    assert "def build(name) -> str" in answer, "the signature, not the file"


async def test_a_name_the_server_does_not_know_says_so(worktree: Path) -> None:
    session = servers(worktree)
    try:
        answer = await session.find_symbol("nonexistent", [Language.PYTHON])
    finally:
        await session.aclose()
    assert "no symbol named 'nonexistent'" in answer
    assert "grep is the fallback" in answer


async def test_references_carry_the_line_that_uses_the_symbol(worktree: Path) -> None:
    session = servers(worktree)
    try:
        answer = await session.find_references("build", [Language.PYTHON])
    finally:
        await session.aclose()
    assert "build is defined at src/app.py:4 and used:" in answer
    assert 'src/app.py:9  return build("x")' in answer, "the call site, read from disk"


async def test_an_outline_lists_a_file_without_reading_it(worktree: Path) -> None:
    session = servers(worktree)
    try:
        answer = await session.outline("src/app.py")
    finally:
        await session.aclose()
    assert "function build" in answer
    assert "function caller" in answer
    assert "variable name" in answer, "children are nested, not dropped"
    assert answer.splitlines()[0].startswith("    4"), "lines count from one"


async def test_an_outline_refuses_a_suffix_no_server_owns(worktree: Path) -> None:
    session = servers(worktree)
    try:
        with pytest.raises(Exception, match="no language server owns"):
            await session.outline("notes.tcl")
    finally:
        await session.aclose()


async def test_an_outline_that_cannot_be_answered_names_the_fallback(
    worktree: Path,
) -> None:
    """The cheap answer is gone; the expensive one is still there.

    Observed on a live run: a 4B spent a call on `file_outline` for a language
    with no server installed, was told only that, and went looking elsewhere.
    `find_symbol` already says "grep still works"; this said nothing.
    """
    owned = servers(worktree)
    try:
        with pytest.raises(Exception, match=r"read notes\.tcl instead"):
            await owned.outline("notes.tcl")
    finally:
        await owned.aclose()

    # The other branch: a suffix a server *would* own, with none to run.
    absent = LanguageServers(worktree, disabled=frozenset({Language.PYTHON}))
    try:
        with pytest.raises(Exception, match=r"read src/app\.py instead"):
            await absent.outline("src/app.py")
    finally:
        await absent.aclose()


# ------------------------------------------------------------- synchronisation


async def test_a_document_is_opened_before_it_is_asked_about(worktree: Path) -> None:
    session = servers(worktree)
    try:
        await session.outline("src/app.py")
    finally:
        await session.aclose()
    # Most servers answer `textDocument/*` only about documents they were
    # given, so the order is the test: opened, then asked.
    traffic = [
        str(entry.get("notification") or entry.get("request")) for entry in received(worktree)
    ]
    assert "textDocument/didOpen" in traffic
    assert traffic.index("textDocument/didOpen") < traffic.index("textDocument/documentSymbol")


async def test_a_patch_tells_the_server_what_changed(worktree: Path) -> None:
    session = servers(worktree)
    try:
        await session.outline("src/app.py")
        (worktree / "src" / "app.py").write_text(
            FIXTURE.replace("return name", "return name.strip()"),
            encoding="utf-8",
        )
        await session.changed(["src/app.py"])
    finally:
        await session.aclose()
    methods = [entry.get("notification") for entry in received(worktree)]
    assert "textDocument/didChange" in methods, "an open document gets the new text"
    assert "workspace/didChangeWatchedFiles" in methods, "and the tree gets the event"
    changes = [
        entry
        for entry in received(worktree)
        if entry.get("notification") == "textDocument/didChange"
    ]
    params = changes[0]["params"]
    assert isinstance(params, dict)
    content = params["contentChanges"]
    assert isinstance(content, list)
    assert "strip()" in str(content[0]), "the text it sends is the text on disk"


# ------------------------------------------------------------ which is used


async def test_the_first_installed_candidate_wins(worktree: Path) -> None:
    # Only the second candidate is "installed", so that is the one chosen -
    # an image ships whichever implementation it has.
    session = LanguageServers(
        worktree,
        which=lambda command: command if command == "pyright-langserver" else None,
    )
    candidate = session.candidate_for(Language.PYTHON)
    assert candidate is not None
    assert candidate.command == "pyright-langserver"


async def test_a_disabled_language_is_not_served_even_when_installed(worktree: Path) -> None:
    session = LanguageServers(
        worktree,
        which=lambda command: command,
        disabled=frozenset({Language.PYTHON}),
    )
    assert session.candidate_for(Language.PYTHON) is None
    assert session.installed([Language.PYTHON, Language.RUST]) == {
        Language.RUST: LanguageServers(worktree, which=lambda command: command).candidate_for(
            Language.RUST,
        ),
    }


async def test_a_configured_command_wins_over_the_shipped_candidates(worktree: Path) -> None:
    session = LanguageServers(
        worktree,
        overrides={Language.PYTHON: ServerCandidate(command=sys.executable, args=("-V",))},
        which=lambda command: command,
    )
    candidate = session.candidate_for(Language.PYTHON)
    assert candidate is not None
    assert candidate.command == sys.executable


# ----------------------------------------------- when the index is not ready


async def test_a_symbol_is_found_even_with_a_cold_index(worktree: Path) -> None:
    # This is the clangd case, measured: `workspace/symbol` answered nothing on
    # a one-file project because the background index was not built yet. The
    # files that mention the name are then asked directly.
    session = servers(worktree, "--no-index")
    try:
        answer = await session.find_symbol("build", [Language.PYTHON])
        references = await session.find_references("build", [Language.PYTHON])
    finally:
        await session.aclose()
    assert "src/app.py:4  function build" in answer
    assert "is defined at src/app.py:4 and used:" in references


async def test_two_uses_on_one_line_are_one_line(worktree: Path) -> None:
    session = servers(worktree)
    try:
        answer = await session.find_references("build", [Language.PYTHON])
    finally:
        await session.aclose()
    assert answer.count("src/app.py:9") == 1, "a second copy of the line teaches nothing"


def test_a_signature_is_preferred_over_a_heading() -> None:
    # clangd answers with a markdown document; pyright with a fenced
    # signature; an old server with a bare string. All three are hover.
    clangd: JsonValue = {
        "contents": {
            "kind": "markdown",
            "value": "### function `checksum`\n\n---\n→ `int`\n\n---\n"
            "```cpp\nint checksum(int a, int b)\n```",
        },
    }
    assert _hover_text(clangd) == "int checksum(int a, int b)"
    pyright: JsonValue = {
        "contents": {
            "kind": "markdown",
            "value": "```python\n(function) def build() -> str\n```",
        }
    }
    assert _hover_text(pyright) == "(function) def build() -> str"
    plain: JsonValue = {"contents": "build(name)"}
    assert _hover_text(plain) == "build(name)"
    assert _hover_text({"contents": []}) == ""
    assert _hover_text(None) == ""

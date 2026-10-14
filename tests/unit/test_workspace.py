# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Worktree operations: confinement, slicing, and the caps."""

from pathlib import Path

import pytest

from niharness.domain.tools import ToolLimits
from niharness.toolserver.workspace import Workspace, WorkspaceError


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text(
        "\n".join(f"line {n}" for n in range(1, 21)), encoding="utf-8"
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return Workspace(root, limits=ToolLimits(max_result_bytes=4_000, max_read_lines=5))


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside", "src/../../outside", "~/.ssh/id"]
)
def test_a_path_outside_the_worktree_is_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceError):
        workspace.resolve(path)


def test_a_path_inside_resolves(workspace: Workspace) -> None:
    assert workspace.resolve("src/a.py").name == "a.py"
    assert workspace.resolve(".") == workspace.root


def test_a_symlink_pointing_out_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("x", encoding="utf-8")
    (workspace.root / "link").symlink_to(secret)
    with pytest.raises(WorkspaceError, match="outside the worktree"):
        workspace.resolve("link")


async def test_reading_is_bounded_by_the_line_cap(workspace: Workspace) -> None:
    body = await workspace.read_file("src/a.py")
    assert "1  line 1" in body
    assert "line 5" in body
    assert "line 6" not in body, "max_read_lines is a cap, not a suggestion"


async def test_reading_a_slice(workspace: Workspace) -> None:
    body = await workspace.read_file("src/a.py", start_line=3, end_line=4)
    assert "3  line 3" in body
    assert "line 2" not in body
    assert "line 5" not in body


async def test_reading_past_the_end_says_so(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="past the end"):
        await workspace.read_file("src/a.py", start_line=500)


async def test_reading_a_directory_is_not_a_file(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="not a file"):
        await workspace.read_file("src")


async def test_listing_is_relative_to_the_worktree(workspace: Workspace) -> None:
    listing = await workspace.list_tree(".")
    assert "README.md" in listing
    assert "src/a.py" in listing
    assert str(workspace.root) not in listing, "absolute paths leak the host layout"


async def test_grep_reports_file_and_line(workspace: Workspace) -> None:
    assert "src/a.py:7:" in await workspace.grep("line 7")


async def test_grep_without_matches_says_so(workspace: Workspace) -> None:
    assert "no matches" in await workspace.grep("nothing-here")


async def test_grep_respects_the_match_cap(workspace: Workspace) -> None:
    found = await workspace.grep("line", max_matches=3)
    assert len(found.splitlines()) == 3


async def test_a_bad_pattern_is_reported_not_raised_as_re_error(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="bad pattern"):
        await workspace.grep("(unclosed")


async def test_an_oversized_diff_is_refused(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceError, match="limit is"):
        await workspace.apply_patch("x" * (ToolLimits().max_diff_bytes + 1))

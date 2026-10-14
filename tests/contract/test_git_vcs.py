# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The git adapter, against a throwaway repository on disk."""

import shutil
import subprocess
from pathlib import Path

import pytest

from niharness.adapters.git import GitError, GitVcs
from niharness.domain.identity import RunId, run_branch

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

RUN = RunId("run0001")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "chore: initial commit\n\nSeed the repository.")
    return root


@pytest.fixture
def vcs(repo: Path) -> GitVcs:
    return GitVcs(repo)


async def test_a_run_gets_its_own_branch_and_directory(vcs: GitVcs, repo: Path) -> None:
    worktree = await vcs.create_worktree(RUN)
    assert worktree.branch == run_branch(RUN)
    assert Path(worktree.path).is_dir()
    assert worktree.base_sha == git(repo, "rev-parse", "HEAD").strip()
    branches = git(repo, "branch", "--list", worktree.branch)
    assert worktree.branch in branches


async def test_the_users_checkout_is_untouched(vcs: GitVcs, repo: Path) -> None:
    worktree = await vcs.create_worktree(RUN)
    (Path(worktree.path) / "new.py").write_text("x = 1\n", encoding="utf-8")
    await vcs.commit(worktree, "feat: add new module\n\nBecause the task asked for it.")
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / "new.py").exists()


async def test_commit_reports_what_landed(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    (Path(worktree.path) / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    commit = await vcs.commit(worktree, "feat(core): add a module\n\nIt is needed by the task.")
    assert commit.subject == "feat(core): add a module"
    assert commit.files_changed == 1
    assert commit.insertions == 2
    assert commit.deletions == 0
    assert commit.sha == await vcs.head(worktree)


async def test_changed_paths_include_untracked_files(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    (Path(worktree.path) / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    await vcs.commit(worktree, "feat: add tracked\n\nBody.")
    (Path(worktree.path) / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    (Path(worktree.path) / "untracked.py").write_text("y = 1\n", encoding="utf-8")
    assert await vcs.changed_paths(worktree) == ("tracked.py", "untracked.py")


async def test_rollback_discards_everything_since_the_green_commit(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    green = await vcs.head(worktree)
    (Path(worktree.path) / "broken.py").write_text("syntax error\n", encoding="utf-8")
    await vcs.rollback(worktree, green)
    assert await vcs.changed_paths(worktree) == ()
    assert not (Path(worktree.path) / "broken.py").exists()


async def test_the_tree_digest_tracks_content_and_comes_back(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    first = await vcs.tree_digest(worktree)
    target = Path(worktree.path) / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    second = await vcs.tree_digest(worktree)
    target.unlink()
    third = await vcs.tree_digest(worktree)
    assert first != second
    assert third == first


async def test_the_tree_digest_leaves_the_index_alone(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    (Path(worktree.path) / "a.py").write_text("x = 1\n", encoding="utf-8")
    await vcs.tree_digest(worktree)
    assert git(Path(worktree.path), "diff", "--cached", "--name-only") == ""


async def test_listing_files_feeds_gate_detection(vcs: GitVcs) -> None:
    worktree = await vcs.create_worktree(RUN)
    assert "pyproject.toml" in await vcs.list_files(worktree)


async def test_removing_a_worktree_keeps_the_branch(vcs: GitVcs, repo: Path) -> None:
    worktree = await vcs.create_worktree(RUN)
    await vcs.remove_worktree(worktree)
    assert not Path(worktree.path).exists()
    assert worktree.branch in git(repo, "branch", "--list", worktree.branch)


async def test_a_failing_git_command_reports_its_argv(tmp_path: Path) -> None:
    vcs = GitVcs(tmp_path)
    with pytest.raises(GitError) as error:
        await vcs.create_worktree(RUN)
    assert error.value.argv[0] == "git"
    assert error.value.exit_code != 0

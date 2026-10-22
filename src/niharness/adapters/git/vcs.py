# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Git operations on a per-run worktree.

The agent never works in the user's checkout: every run gets a worktree on
`harness/run-<id>`, and merging stays a human action. The adapter shells out to
`git` with fixed argument vectors - no shell, no interpolation.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import structlog

from niharness.domain.identity import Digest, RunId, digest_text, run_branch
from niharness.ports.vcs import Commit, Worktree

_log = structlog.get_logger(__name__)


SELF_IGNORE = "*\n"
"""A `.gitignore` holding `*` ignores its own directory, itself included."""


def _ignore_self(directory: Path) -> None:
    """Keep harness state out of the user's `git status` without editing their ignore file."""
    marker = directory / ".gitignore"
    if not marker.exists():
        marker.write_text(SELF_IGNORE, encoding="utf-8")


class GitError(RuntimeError):
    """A git command failed."""

    def __init__(self, argv: Sequence[str], exit_code: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} exited {exit_code}: {stderr.strip()}")


class GitVcs:
    """A :class:`niharness.ports.vcs.Vcs` implementation over the git CLI."""

    def __init__(self, repo_root: Path, worktree_root: Path | None = None) -> None:
        """Create an adapter for the repository at `repo_root`.

        Args:
            repo_root: the user's checkout; it is read, never modified.
            worktree_root: where run worktrees are created; defaults to
                `<repo_root>/.nih/worktrees`.
        """
        self._repo_root = repo_root
        self._worktree_root = worktree_root or (repo_root / ".nih" / "worktrees")

    async def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run one git command and return its stdout.

        Raises:
            GitError: when git exits non-zero.
        """
        argv = ("git", *args)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd or self._repo_root),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
        out, err = await process.communicate(
            stdin.encode("utf-8") if stdin is not None else None
        )
        if process.returncode != 0:
            raise GitError(argv, process.returncode or -1, err.decode("utf-8", "replace"))
        return out.decode("utf-8", "replace")

    async def create_worktree(self, run_id: RunId) -> Worktree:
        """Create the run's worktree and branch from the current HEAD."""
        base_sha = (await self._git("rev-parse", "HEAD")).strip()
        branch = run_branch(run_id)
        path = self._worktree_root / run_id
        self._worktree_root.mkdir(parents=True, exist_ok=True)
        _ignore_self(self._worktree_root)
        await self._git("worktree", "add", "-b", branch, str(path), base_sha)
        _log.info("vcs.worktree_created", branch=branch, path=str(path), base=base_sha)
        return Worktree(path=str(path), branch=branch, base_sha=base_sha)

    async def open_worktree(self, run_id: RunId) -> Worktree:
        """Return the worktree `run_id` is already using.

        Raises:
            GitError: when the directory is gone, which is what a resume has to
                be told rather than work around.
        """
        path = self._worktree_root / run_id
        branch = run_branch(run_id)
        if not (path / ".git").exists():
            raise GitError(
                ("git", "worktree", "open", str(path)), 1, f"{path} is not a worktree"
            )
        base = (await self._git("rev-parse", "HEAD", cwd=path)).strip()
        return Worktree(path=str(path), branch=branch, base_sha=base)

    async def remove_worktree(self, worktree: Worktree) -> None:
        """Remove the worktree directory, leaving the branch for inspection."""
        await self._git("worktree", "remove", "--force", worktree.path)

    async def commit(self, worktree: Worktree, message: str) -> Commit:
        """Stage everything and commit. The caller has already run the gate."""
        path = Path(worktree.path)
        await self._git("add", "-A", cwd=path)
        await self._git("commit", "-F", "-", cwd=path, stdin=message)
        sha = (await self._git("rev-parse", "HEAD", cwd=path)).strip()
        stat = await self._git("show", "--numstat", "--format=", sha, cwd=path)
        files = insertions = deletions = 0
        for line in stat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            files += 1
            added, removed, _ = parts
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        subject = message.strip().splitlines()[0]
        _log.info("vcs.committed", sha=sha, subject=subject, files=files)
        return Commit(
            sha=sha,
            subject=subject,
            files_changed=files,
            insertions=insertions,
            deletions=deletions,
        )

    async def rollback(self, worktree: Worktree, to_sha: str) -> None:
        """Discard every change back to `to_sha`, the last green commit."""
        path = Path(worktree.path)
        await self._git("reset", "--hard", to_sha, cwd=path)
        await self._git("clean", "-fd", cwd=path)
        _log.warning("vcs.rolled_back", to_sha=to_sha, path=worktree.path)

    async def head(self, worktree: Worktree) -> str:
        """Return the worktree's current commit."""
        return (await self._git("rev-parse", "HEAD", cwd=Path(worktree.path))).strip()

    async def changed_paths(self, worktree: Worktree) -> tuple[str, ...]:
        """Return the paths changed since HEAD, sorted and worktree-relative."""
        status = await self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            cwd=Path(worktree.path),
        )
        paths: set[str] = set()
        for line in status.splitlines():
            if len(line) < 4:
                continue
            entry = line[3:]
            if " -> " in entry:
                _, _, renamed = entry.partition(" -> ")
                paths.add(renamed.strip().strip('"'))
            else:
                paths.add(entry.strip().strip('"'))
        return tuple(sorted(paths))

    async def diff(self, worktree: Worktree, revision: str = "HEAD") -> str:
        """Return the diff `revision` introduced, with its message."""
        return await self._git(
            "show",
            "--format=%H%n%s%n%n%b",
            "--patch",
            revision,
            cwd=Path(worktree.path),
        )

    async def tree_digest(self, worktree: Worktree) -> Digest:
        """Return a digest of the working tree, including untracked files.

        A temporary index is used, so the real index is never disturbed: the
        digest is a side-effect-free observation that the state-cycle detector
        can rely on.
        """
        path = Path(worktree.path)
        with tempfile.TemporaryDirectory(prefix="nih-index-") as scratch:
            index = str(Path(scratch) / "index")
            env = {"GIT_INDEX_FILE": index}
            await self._git("add", "-A", cwd=path, env=env)
            tree = (await self._git("write-tree", cwd=path, env=env)).strip()
        return digest_text(tree)

    async def list_files(self, worktree: Worktree) -> tuple[str, ...]:
        """Return the tracked paths, for gate language detection."""
        listing = await self._git(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            cwd=Path(worktree.path),
        )
        return tuple(sorted(line for line in listing.splitlines() if line))

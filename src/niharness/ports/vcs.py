# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The version control port.

An agent works in a dedicated worktree on `harness/run-<id>`; the user's
checkout is never touched, and merging is a human action.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.identity import Digest, RunId


class Worktree(BaseModel):
    """A checkout dedicated to one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    branch: str
    base_sha: str = Field(description="The commit the worktree branched from.")


class Commit(BaseModel):
    """A commit that passed the gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    subject: str
    files_changed: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)


@runtime_checkable
class Vcs(Protocol):
    """Git operations the harness needs, and no others."""

    async def create_worktree(self, run_id: RunId) -> Worktree:
        """Create the run's worktree and branch, based on the current HEAD."""
        ...

    async def remove_worktree(self, worktree: Worktree) -> None:
        """Remove a worktree, leaving its branch in place for inspection."""
        ...

    async def commit(self, worktree: Worktree, message: str) -> Commit:
        """Commit everything staged in `worktree`. The caller has already run the gate."""
        ...

    async def rollback(self, worktree: Worktree, to_sha: str) -> None:
        """Discard working-tree changes back to `to_sha`, the last green commit."""
        ...

    async def head(self, worktree: Worktree) -> str:
        """Return the current commit sha."""
        ...

    async def changed_paths(self, worktree: Worktree) -> tuple[str, ...]:
        """Return the paths changed since HEAD, worktree-relative and sorted."""
        ...

    async def diff(self, worktree: Worktree, revision: str = "HEAD") -> str:
        """Return the diff a commit introduced.

        The reviewer starts from this. It still re-runs the build and the tests
        itself; what the diff saves is the reviewer having to reconstruct what
        changed from the task description.
        """
        ...

    async def tree_digest(self, worktree: Worktree) -> Digest:
        """Return a digest of the working tree, used by the state-cycle detector."""
        ...

    async def list_files(self, worktree: Worktree) -> tuple[str, ...]:
        """Return the tracked paths, for gate language detection."""
        ...

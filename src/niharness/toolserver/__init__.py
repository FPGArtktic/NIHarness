# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool server: what the harness talks to, running inside the sandbox.

The harness is an MCP client; this is the other end. It runs in the container,
sees exactly one writable directory - the run's worktree, mounted at `/work` -
and refuses anything outside it. The gate lives here too: `git_commit` is not a
commit, it is an attempt that format, lint, build and the quick tests have to
survive.
"""

from niharness.toolserver.workspace import Workspace, WorkspaceError

__all__ = ["Workspace", "WorkspaceError"]

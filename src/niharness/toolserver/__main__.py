# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
r"""Run the tool server over stdio.

This is what the sandbox container starts:

    podman run -i --network=none --volume <worktree>:/work:rw,Z ... \\
        localhost/niharness-sandbox:latest \\
        python -m niharness.toolserver --root /work

Standard output belongs to the protocol, so logs go to standard error.

The worktree is a checkout of the project, so `harness.toml` is in it: the
server reads its own `[lsp]` settings from there rather than having them
threaded through an argument vector. A worktree without one gets the defaults.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import structlog
from pydantic import ValidationError

from niharness.cli.logging import configure
from niharness.config import HarnessConfig, LspConfig
from niharness.domain.language_servers import ServerCandidate
from niharness.toolserver.lsp import LanguageServers
from niharness.toolserver.server import build_server, workspace_for
from niharness.toolserver.workspace import Workspace

_log = structlog.get_logger(__name__)


def lsp_settings(root: Path) -> LspConfig:
    """Return the project's language-server settings, or the defaults.

    A malformed or absent `harness.toml` must not stop the tool server: the
    gate and the file tools matter more than the symbol tools, and the model is
    told plainly when a symbol tool has nothing to ask.
    """
    try:
        return HarnessConfig.load(root).lsp
    except (ValidationError, OSError, ValueError) as error:
        _log.warning("toolserver.config_unreadable", root=str(root), error=str(error))
        return LspConfig()


def language_servers(root: Path, settings: LspConfig) -> LanguageServers | None:
    """Return the language-server session this worktree gets, or None when off."""
    if not settings.enabled:
        return None
    return LanguageServers(
        root,
        overrides={
            language: ServerCandidate(command=server.command, args=server.args)
            for language, server in settings.servers.items()
        },
        disabled=frozenset(settings.disabled_languages),
        request_timeout=settings.request_timeout_seconds,
        start_timeout=settings.start_timeout_seconds,
    )


async def serve(workspace: Workspace) -> None:
    """Serve until the client disconnects, then stop the language servers."""
    try:
        await build_server(workspace).run_stdio_async()
    finally:
        # The same event loop that started them has to stop them, which is why
        # this is not a callback after a blocking run().
        await workspace.aclose()


def main() -> None:
    """Parse arguments and serve until the client disconnects."""
    parser = argparse.ArgumentParser(prog="niharness.toolserver", description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path("/work"), help="the worktree to serve"
    )
    parser.add_argument(
        "--no-lsp",
        action="store_true",
        help="do not start language servers, whatever harness.toml says",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging on stderr")
    arguments = parser.parse_args()
    configure(verbose=arguments.verbose)
    root = arguments.root.resolve()
    if not root.is_dir():
        parser.error(f"{root} is not a directory")
    settings = lsp_settings(root)
    servers = None if arguments.no_lsp else language_servers(root, settings)
    asyncio.run(serve(workspace_for(root, servers=servers)))


if __name__ == "__main__":
    main()

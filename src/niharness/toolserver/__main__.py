# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
r"""Run the tool server over stdio.

This is what the sandbox container starts:

    podman run -i --network=none --volume <worktree>:/work:rw,Z ... \\
        localhost/niharness-sandbox:latest \\
        python -m niharness.toolserver --root /work

Standard output belongs to the protocol, so logs go to standard error.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from niharness.cli.logging import configure
from niharness.toolserver.server import server_for


def main() -> None:
    """Parse arguments and serve until the client disconnects."""
    parser = argparse.ArgumentParser(prog="niharness.toolserver", description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path("/work"), help="the worktree to serve"
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging on stderr")
    arguments = parser.parse_args()
    configure(verbose=arguments.verbose)
    root = arguments.root.resolve()
    if not root.is_dir():
        parser.error(f"{root} is not a directory")
    server_for(root).run("stdio")


if __name__ == "__main__":
    main()

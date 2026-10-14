# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih gate`: show what the gate would run here.

The gate itself executes in the sandbox. This command answers the question a
human actually asks first - "what will it check?" - without a container.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from niharness.adapters.git import GitError, GitVcs
from niharness.domain.gate import GateTier, detect_languages, plan_gate
from niharness.ports.vcs import Worktree


async def _tracked_files(root: Path) -> tuple[str, ...]:
    """Return the tracked and untracked-but-not-ignored files of `root`."""
    vcs = GitVcs(root)
    worktree = Worktree(path=str(root), branch="", base_sha="")
    return await vcs.list_files(worktree)


def gate(root: Path, tier: GateTier, changed: tuple[str, ...]) -> None:
    """Print the gate plan for `root`.

    Raises:
        typer.Exit: when the directory is not a git repository.
    """
    try:
        files = asyncio.run(_tracked_files(root))
    except GitError as error:
        typer.secho(f"not a git repository: {root}", fg=typer.colors.RED, err=True)
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
    languages = detect_languages(files)
    commands = plan_gate(tier, languages, changed)
    typer.echo(f"tier: {tier.value}")
    typer.echo(
        f"languages: {', '.join(sorted(item.value for item in languages)) or 'none detected'}"
    )
    if changed:
        typer.echo(f"changed paths considered: {len(changed)}")
    typer.echo(f"{len(commands)} command(s):")
    for command in commands:
        typer.echo(f"  [{command.kind.value:6}] {command.name:18} {' '.join(command.argv)}")

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
from niharness.cli.output import emit, fail
from niharness.domain.gate import GateTier, detect_languages, gate_listing, plan_gate
from niharness.ports.vcs import Worktree


async def _tracked_files(root: Path) -> tuple[str, ...]:
    """Return the tracked and untracked-but-not-ignored files of `root`."""
    vcs = GitVcs(root)
    worktree = Worktree(path=str(root), branch="", base_sha="")
    return await vcs.list_files(worktree)


def gate(
    root: Path, tier: GateTier, changed: tuple[str, ...], *, json_mode: bool = False
) -> None:
    """Print the gate plan for `root`.

    Raises:
        typer.Exit: when the directory is not a git repository.
    """
    try:
        files = asyncio.run(_tracked_files(root))
    except GitError as error:
        fail(f"not a git repository: {root}", {"detail": str(error)}, json_mode=json_mode)
        raise typer.Exit(1) from error
    languages = detect_languages(files)
    pyproject = root / "pyproject.toml"
    listing = gate_listing(
        files,
        pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else "",
    )
    commands = plan_gate(tier, languages, changed, listing)
    human = [
        f"tier: {tier.value}",
        f"languages: {', '.join(sorted(item.value for item in languages)) or 'none detected'}",
    ]
    if changed:
        human.append(f"changed paths considered: {len(changed)}")
    human.append(f"{len(commands)} command(s):")
    human += [
        f"  [{command.kind.value:6}] {command.name:18} {' '.join(command.argv)}"
        for command in commands
    ]
    emit(
        {
            "ok": True,
            "tier": tier.value,
            "languages": sorted(item.value for item in languages),
            "commands": [
                {"name": command.name, "kind": command.kind.value, "argv": list(command.argv)}
                for command in commands
            ],
        },
        json_mode=json_mode,
        human="\n".join(human),
    )

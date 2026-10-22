# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih runs` and `nih cancel`: the registry and the control inbox, for a person."""

from __future__ import annotations

import asyncio

import typer

from niharness.cli.output import emit, fail
from niharness.config import HarnessConfig
from niharness.domain.control import Driver
from niharness.management.runs import NoSuchRunError, RunEntry, RunRegistry


def render(entries: tuple[RunEntry, ...]) -> str:
    """Render the registry for a person."""
    if not entries:
        return "no runs"
    width = max(len(entry.run_id) for entry in entries)
    return "\n".join(
        f"{'live' if entry.live else '    '}  {entry.run_id.ljust(width)}  "
        f"{entry.outcome:9}  stage {entry.stage:12}  {entry.events} events  "
        f"{entry.commits} commit(s)"
        for entry in entries
    )


def runs(config: HarnessConfig, *, json_mode: bool) -> None:
    """List this project's runs."""
    entries = asyncio.run(RunRegistry(config).collect())
    emit(
        {"ok": True, "runs": [entry.model_dump(mode="json") for entry in entries]},
        json_mode=json_mode,
        human=render(entries),
    )


def cancel(config: HarnessConfig, run_id: str, reason: str, *, json_mode: bool) -> None:
    """Ask a run to stop at its next step boundary.

    Raises:
        typer.Exit: when there is no such run.
    """
    registry = RunRegistry(config)
    try:
        resolved = registry.require(run_id)
    except NoSuchRunError as error:
        fail(f"no run {run_id}", {"run_id": run_id}, json_mode=json_mode)
        raise typer.Exit(1) from error
    message = asyncio.run(registry.cancel(resolved, reason, Driver.CLI))
    emit(
        {"ok": True, "run_id": resolved, "submitted": message.id, "kind": message.kind},
        json_mode=json_mode,
        human=f"cancel requested for {resolved}; it stops at the next step boundary",
    )

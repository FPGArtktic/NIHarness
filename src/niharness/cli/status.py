# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih status`: fold a run's event log into a snapshot."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.domain.events import Event
from niharness.domain.identity import RunId
from niharness.domain.state import RunSnapshot, fold


def list_runs(runs_dir: Path) -> tuple[RunId, ...]:
    """Return the run ids that have an event log under `runs_dir`."""
    if not runs_dir.is_dir():
        return ()
    return tuple(
        RunId(entry.name)
        for entry in sorted(runs_dir.iterdir())
        if (entry / "events.jsonl").is_file()
    )


async def read_snapshot(runs_dir: Path, run_id: RunId) -> RunSnapshot:
    """Replay a run's events and fold them into a snapshot."""
    sink = JsonlEventSink(runs_dir, run_id)
    stream: list[Event] = [event async for event in sink.replay(run_id)]
    return fold(stream, run_id)


def render(snapshot: RunSnapshot) -> str:
    """Render a snapshot as aligned key-value lines."""
    rows: list[tuple[str, str]] = [
        ("run", snapshot.run_id),
        ("events", str(snapshot.seq)),
        ("stage", snapshot.stage or "-"),
        ("outcome", snapshot.outcome.value if snapshot.outcome else "running"),
        ("tokens", str(snapshot.tokens_used)),
        ("tool calls", f"{snapshot.tool_calls} ({snapshot.rejected_calls} rejected)"),
        ("gate", f"{snapshot.gate_runs} runs, {snapshot.gate_failures} red"),
        ("commits", str(len(snapshot.commits))),
        ("compactions", str(snapshot.compactions)),
        ("max intervention", str(snapshot.highest_intervention)),
        ("open questions", str(snapshot.open_questions)),
        ("context fill", f"{snapshot.context_fill:.0%}"),
    ]
    width = max(len(key) for key, _ in rows)
    return "\n".join(f"{key.ljust(width)}  {value}" for key, value in rows)


def status(runs_dir: Path, run_id: str | None) -> None:
    """Print the status of one run, or list the runs that exist.

    Raises:
        typer.Exit: when the named run has no event log.
    """
    runs = list_runs(runs_dir)
    if run_id is None:
        if not runs:
            typer.echo(f"no runs under {runs_dir}")
            return
        typer.echo(f"{len(runs)} run(s) under {runs_dir}:")
        for candidate in runs:
            typer.echo(f"  {candidate}")
        return
    if RunId(run_id) not in runs:
        typer.secho(f"no event log for run {run_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(render(asyncio.run(read_snapshot(runs_dir, RunId(run_id)))))

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih status`: fold a run's event log into a snapshot."""

from __future__ import annotations

import asyncio
from pathlib import Path

from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.cli.output import emit, fail
from niharness.config import HarnessConfig
from niharness.domain.events import Event
from niharness.domain.identity import RunId
from niharness.domain.state import RunSnapshot, fold
from niharness.management.runs import RunRegistry


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


def status(config: HarnessConfig, run_id: str | None, *, json_mode: bool = False) -> None:
    """Print the status of one run, or list the runs that exist.

    Raises:
        typer.Exit: when the named run has no event log.
    """
    runs_dir = config.runs_dir
    registry = RunRegistry(config)
    runs = registry.ids()
    if run_id is None:
        emit(
            {"ok": True, "runs": list(runs)},
            json_mode=json_mode,
            human=(
                f"no runs under {runs_dir}"
                if not runs
                else f"{len(runs)} run(s) under {runs_dir}:\n"
                + "\n".join(f"  {candidate}" for candidate in runs)
            ),
        )
        return
    if RunId(run_id) not in runs:
        fail(f"no event log for run {run_id}", {"run_id": run_id}, json_mode=json_mode)
    snapshot = asyncio.run(read_snapshot(runs_dir, RunId(run_id)))
    emit(
        {
            "ok": True,
            "live": registry.is_live(RunId(run_id)),
            "snapshot": snapshot.model_dump(mode="json"),
        },
        json_mode=json_mode,
        human=render(snapshot),
    )

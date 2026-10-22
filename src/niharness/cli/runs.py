# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih runs` and `nih cancel`: the run registry and the control inbox.

Both are deliberately cheap to call. A supervisor - a person, or an agent
spending tokens on every line it reads - should be able to ask what is
happening without being handed a log.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from niharness.adapters.jsonl_control import JsonlControlChannel
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.cli.output import emit, fail
from niharness.domain.control import CancelRun, Driver
from niharness.domain.events import Event
from niharness.domain.identity import RunId
from niharness.domain.state import RunSnapshot, fold

PID_FILE = "run.pid"


class RunEntry(BaseModel):
    """One row of the registry: what a run is, without reading its log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    live: bool
    stage: str
    outcome: str
    events: int
    tool_calls: int
    commits: int
    context_fill: float


def list_run_ids(runs_dir: Path) -> tuple[RunId, ...]:
    """Return the run ids that have an event log, newest id last."""
    if not runs_dir.is_dir():
        return ()
    return tuple(
        RunId(entry.name)
        for entry in sorted(runs_dir.iterdir())
        if (entry / "events.jsonl").is_file()
    )


def is_live(runs_dir: Path, run_id: RunId) -> bool:
    """True when a process is still running this run.

    The pid file is written by `nih implement` and removed when it returns, so
    a stale file after a crash is detected by asking the kernel rather than by
    trusting the file.
    """
    pid_path = runs_dir / run_id / PID_FILE
    if not pid_path.is_file():
        return False
    text = pid_path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        return False
    try:
        os.kill(int(text), 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


async def snapshot_of(runs_dir: Path, run_id: RunId) -> RunSnapshot:
    """Fold a run's event log."""
    sink = JsonlEventSink(runs_dir, run_id)
    stream: list[Event] = [event async for event in sink.replay(run_id)]
    return fold(stream, run_id)


async def collect(runs_dir: Path) -> tuple[RunEntry, ...]:
    """Fold every run into one registry row each."""
    entries: list[RunEntry] = []
    for run_id in list_run_ids(runs_dir):
        snapshot = await snapshot_of(runs_dir, run_id)
        entries.append(
            RunEntry(
                run_id=run_id,
                live=is_live(runs_dir, run_id),
                stage=snapshot.stage or "-",
                outcome=snapshot.outcome.value if snapshot.outcome else "running",
                events=snapshot.seq,
                tool_calls=snapshot.tool_calls,
                commits=len(snapshot.commits),
                context_fill=snapshot.context_fill,
            ),
        )
    return tuple(entries)


def render(entries: tuple[RunEntry, ...]) -> str:
    """Render the registry for a person."""
    if not entries:
        return "no runs"
    width = max(len(entry.run_id) for entry in entries)
    lines = []
    for entry in entries:
        mark = "live" if entry.live else "    "
        lines.append(
            f"{mark}  {entry.run_id.ljust(width)}  {entry.outcome:9}  "
            f"stage {entry.stage:12}  {entry.events} events  "
            f"{entry.commits} commit(s)",
        )
    return "\n".join(lines)


def runs(runs_dir: Path, *, json_mode: bool) -> None:
    """List the runs under `runs_dir`."""
    entries = asyncio.run(collect(runs_dir))
    emit(
        {"ok": True, "runs": [entry.model_dump(mode="json") for entry in entries]},
        json_mode=json_mode,
        human=render(entries),
    )


def cancel(runs_dir: Path, run_id: str, reason: str, *, json_mode: bool) -> None:
    """Ask a run to stop at its next step boundary.

    Raises:
        typer.Exit: when there is no such run.
    """
    if RunId(run_id) not in list_run_ids(runs_dir):
        fail(f"no run {run_id}", {"run_id": run_id}, json_mode=json_mode)
    channel = JsonlControlChannel(runs_dir, RunId(run_id))
    message = asyncio.run(
        channel.submit(CancelRun(run_id=RunId(run_id), reason=reason, source=Driver.CLI)),
    )
    emit(
        {"ok": True, "run_id": run_id, "submitted": message.id, "kind": message.kind},
        json_mode=json_mode,
        human=f"cancel requested for {run_id}; it stops at the next step boundary",
    )


@contextlib.contextmanager
def pid_file(runs_dir: Path, run_id: RunId) -> Iterator[None]:
    """Record this process as the owner of `run_id` while it runs."""
    path = runs_dir / run_id / PID_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()

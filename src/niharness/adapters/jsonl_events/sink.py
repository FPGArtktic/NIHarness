# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""An event sink backed by one JSONL file per run.

The file is the source of truth for a run: one event per line, appended and
fsynced before the call returns, never rewritten. Compaction does not touch it,
and a resume replays it. Snapshots are an optimisation, not state.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from niharness.domain.events import Event, EventBody
from niharness.domain.identity import RunId, StepId
from niharness.domain.state import RunSnapshot

EVENTS_FILE: Final = "events.jsonl"
SNAPSHOT_DIR: Final = "snapshots"

_log = structlog.get_logger(__name__)


class JsonlEventSink:
    """Append-only event storage under `<root>/<run_id>/`.

    Implements :class:`niharness.ports.event_sink.EventSink`.
    """

    def __init__(self, root: Path, run_id: RunId) -> None:
        """Create a sink for `run_id` under `root`, creating directories as needed.

        The root is made self-ignoring, so run state never shows up in the
        user's `git status`.
        """
        self._root = root
        self._run_id = run_id
        self._lock = asyncio.Lock()
        self._seq: int | None = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        marker = self._root / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf-8")

    @property
    def run_dir(self) -> Path:
        """The run's directory."""
        return self._root / self._run_id

    @property
    def events_path(self) -> Path:
        """The run's event log."""
        return self.run_dir / EVENTS_FILE

    @property
    def snapshot_dir(self) -> Path:
        """Where snapshots are stored, one file per sequence number."""
        return self.run_dir / SNAPSHOT_DIR

    def _last_seq(self) -> int:
        """Return the sequence number of the last stored event, or zero."""
        if not self.events_path.exists():
            return 0
        last = 0
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = int(json.loads(line)["seq"])
        return last

    def _write(self, event: Event) -> None:
        """Append one event and fsync, so a crash cannot lose it."""
        line = event.model_dump_json(exclude_none=False)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def append(self, body: EventBody, *, step_id: StepId | None = None) -> Event:
        """Append `body` as the next event of this run."""
        async with self._lock:
            if self._seq is None:
                self._seq = await asyncio.to_thread(self._last_seq)
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=datetime.now(UTC),
                run_id=self._run_id,
                step_id=step_id,
                body=body,
            )
            await asyncio.to_thread(self._write, event)
        _log.debug("event.appended", seq=event.seq, type=event.type, run_id=self._run_id)
        return event

    async def replay(self, run_id: RunId) -> AsyncIterator[Event]:
        """Yield every stored event of `run_id`, in sequence order."""
        path = self._root / run_id / EVENTS_FILE
        if not path.exists():
            return
        lines = await asyncio.to_thread(path.read_text, "utf-8")
        for line in lines.splitlines():
            if line.strip():
                yield Event.model_validate_json(line)

    async def write_snapshot(self, snapshot: RunSnapshot) -> None:
        """Store `snapshot` under its sequence number."""
        path = self.snapshot_dir / f"{snapshot.seq:012d}.json"
        payload = snapshot.model_dump_json(indent=2)
        await asyncio.to_thread(path.write_text, payload, "utf-8")

    async def read_snapshot(self, run_id: RunId) -> RunSnapshot | None:
        """Return the newest snapshot of `run_id`, or None when there is none."""
        directory = self._root / run_id / SNAPSHOT_DIR
        if not directory.exists():
            return None
        candidates = sorted(directory.glob("*.json"))
        if not candidates:
            return None
        payload = await asyncio.to_thread(candidates[-1].read_text, "utf-8")
        return RunSnapshot.model_validate_json(payload)

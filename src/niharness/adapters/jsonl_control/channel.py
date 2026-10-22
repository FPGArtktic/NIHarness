# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""A control inbox backed by one JSONL file per run.

Many writers, one reader. Each submission is a single `write` of one line
opened with `O_APPEND`, which the kernel keeps whole, so two drivers submitting
at the same moment produce two intact lines in arrival order. The reader keeps
its position in a cursor file next to the inbox; the run is the only reader, so
that cursor has the single writer the event log also relies on.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

from niharness.domain.control import ControlCommand, ControlMessage, message_id
from niharness.domain.identity import RunId

CONTROL_FILE: Final = "control.jsonl"
CURSOR_FILE: Final = "control.cursor"

_log = structlog.get_logger(__name__)


class JsonlControlChannel:
    """A :class:`niharness.ports.control_channel.ControlChannel` over a JSONL file."""

    def __init__(self, root: Path, run_id: RunId) -> None:
        """Open the inbox of `run_id` under `root`, creating it if needed."""
        self._root = root
        self._run_id = run_id
        self._lock = asyncio.Lock()
        self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_dir(self) -> Path:
        """The run's directory."""
        return self._root / self._run_id

    @property
    def inbox_path(self) -> Path:
        """The inbox file."""
        return self.run_dir / CONTROL_FILE

    @property
    def cursor_path(self) -> Path:
        """Where the reader keeps its position."""
        return self.run_dir / CURSOR_FILE

    def _append(self, line: str) -> None:
        """Append one line durably, as a single write."""
        with self.inbox_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def submit(self, command: ControlCommand) -> ControlMessage:
        """Append `command` to the inbox."""
        now = datetime.now(UTC)
        message = ControlMessage(id=message_id(command, now), ts=now, command=command)
        await asyncio.to_thread(self._append, message.model_dump_json())
        _log.info(
            "control.submitted",
            run_id=self._run_id,
            kind=message.kind,
            source=command.source.value,
        )
        return message

    def _read_cursor(self) -> int:
        """Return how many lines have already been consumed."""
        if not self.cursor_path.exists():
            return 0
        text = self.cursor_path.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else 0

    def _consume(self) -> tuple[ControlMessage, ...]:
        """Read everything past the cursor and move it."""
        if not self.inbox_path.exists():
            return ()
        lines = [
            line
            for line in self.inbox_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        start = self._read_cursor()
        fresh = lines[start:]
        if not fresh:
            return ()
        self.cursor_path.write_text(str(len(lines)), encoding="utf-8")
        messages: list[ControlMessage] = []
        for line in fresh:
            try:
                messages.append(ControlMessage.model_validate_json(line))
            except ValueError:
                # A malformed line is dropped rather than stalling the run; the
                # file keeps it, so nothing is lost for a human reading later.
                _log.warning("control.unreadable", run_id=self._run_id, line=line[:120])
        return tuple(messages)

    async def drain(self) -> tuple[ControlMessage, ...]:
        """Consume everything that arrived since the last drain."""
        async with self._lock:
            return await asyncio.to_thread(self._consume)

    async def pending(self) -> int:
        """Return how many messages are waiting."""
        if not self.inbox_path.exists():
            return 0
        lines = await asyncio.to_thread(self.inbox_path.read_text, "utf-8")
        total = len([line for line in lines.splitlines() if line.strip()])
        return max(0, total - await asyncio.to_thread(self._read_cursor))

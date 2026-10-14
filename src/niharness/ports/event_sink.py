# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The event sink port.

Everything is an event. The sink owns the clock and the sequence counter, which
is why the domain can stay deterministic: it produces payloads, the sink turns
them into positioned, timestamped events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from niharness.domain.events import Event, EventBody
from niharness.domain.identity import RunId, StepId
from niharness.domain.state import RunSnapshot


@runtime_checkable
class EventSink(Protocol):
    """Append-only storage for a run's event stream, plus its snapshots."""

    async def append(self, body: EventBody, *, step_id: StepId | None = None) -> Event:
        """Append one payload and return the stored event.

        The implementation assigns the sequence number and the timestamp, and
        must make the write durable before returning, so a crash cannot lose
        an event the harness already acted on.
        """
        ...

    def replay(self, run_id: RunId) -> AsyncIterator[Event]:
        """Yield every event of `run_id` in sequence order."""
        ...

    async def write_snapshot(self, snapshot: RunSnapshot) -> None:
        """Store a snapshot so a resume does not have to replay from zero."""
        ...

    async def read_snapshot(self, run_id: RunId) -> RunSnapshot | None:
        """Return the newest snapshot of `run_id`, or None when there is none."""
        ...

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The control channel port.

The event log has exactly one writer - the run - because sequence numbers are
assigned in memory. Control therefore does not go into the log: it goes into an
inbox that anyone may append to, and the run drains it and writes the resulting
events itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from niharness.domain.control import ControlCommand, ControlMessage


@runtime_checkable
class ControlChannel(Protocol):
    """An append-only inbox of commands for one run."""

    async def submit(self, command: ControlCommand) -> ControlMessage:
        """Append a command and return the stored message.

        Any number of processes may submit at once; ordering is arrival order
        and nothing more is promised.
        """
        ...

    async def drain(self) -> tuple[ControlMessage, ...]:
        """Return the messages that arrived since the last drain, and consume them.

        The consumer is the run itself, and it is the only one that drains, so
        the cursor is single-writer like the event log.
        """
        ...

    async def pending(self) -> int:
        """Return how many messages are waiting, without consuming them."""
        ...

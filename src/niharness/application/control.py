# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draining the control inbox.

Three drivers steer a run - a person at the TUI, `nih cancel`, an agent over
`nih mcp` - and none of them may write the event log, because the log has one
writer and its sequence numbers are assigned in memory. So they append
requests to the run's inbox, and the run turns those requests into events
itself, between actions.

This is the one place that conversion happens. It matters that there is only
one: the inbox says what was asked, the event log says what the run acted on,
and a command nobody drained never happened. Two implementations of that
sentence would eventually disagree about which.
"""

from __future__ import annotations

import structlog

from niharness.domain import events as ev
from niharness.domain.control import AnswerQuestions, ApprovePlan, CancelRun
from niharness.domain.identity import RunId, digest_json
from niharness.ports.control_channel import ControlChannel
from niharness.ports.event_sink import EventSink

_log = structlog.get_logger(__name__)


class ControlDrain:
    """Turns the inbox's requests into this run's events."""

    def __init__(self, events: EventSink, control: ControlChannel | None) -> None:
        """Bind to a run's event sink and its inbox.

        Args:
            events: the run's sink. The drain is inside the single writer, so
                it appends directly.
            control: the inbox. None means nothing can steer this run, which
                is how a test with no channel and an embedded run both behave.
        """
        self._events = events
        self._control = control
        self._cancelled: str | None = None

    @property
    def listening(self) -> bool:
        """True when there is an inbox to drain at all."""
        return self._control is not None

    @property
    def cancelled(self) -> str | None:
        """Why the run was asked to stop, or None while it was not."""
        return self._cancelled

    async def stop_requested(self, run_id: RunId) -> str | None:
        """Drain the inbox and return a reason to stop, if one arrived.

        A cancel is sticky: once a run has been asked to stop it stays asked,
        so a later drain that finds an empty inbox does not un-cancel it.

        Args:
            run_id: this run. A command addressed to another run is left
                alone rather than acted on - one inbox per run is the
                convention, not a guarantee the format can make.

        Returns:
            The reason to stop, or None to carry on.
        """
        if self._control is None:
            return self._cancelled
        for message in await self._control.drain():
            command = message.command
            if isinstance(command, CancelRun) and command.run_id == run_id:
                self._cancelled = command.reason or f"cancelled from {command.source.value}"
                await self._events.append(ev.RunCancelled(source=command.source.value))
                _log.warning("run.cancel_requested", run_id=run_id, source=command.source.value)
            elif isinstance(command, ApprovePlan):
                await self._events.append(ev.PlanApproved(plan_digest=command.plan_digest))
            elif isinstance(command, AnswerQuestions) and command.run_id == run_id:
                await self._events.append(
                    ev.QuestionsAnswered(
                        answers_digest=digest_json(
                            [answer.model_dump(mode="json") for answer in command.answers],
                        ),
                        count=len(command.answers),
                    ),
                )
        return self._cancelled

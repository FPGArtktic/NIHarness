# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Control commands: what a driver outside the run may ask it to do.

A run is driven by whoever is there - a human at the TUI, Claude over MCP, a
script. They all ask for the same three things, and they all ask for them the
same way, so there is one mechanism and one audit trail instead of one per
driver.

A command is not an event. It is a *request*, and the run turns it into an
event when it acts on it: that keeps the event log's single writer, and it
keeps the log a record of what happened rather than of what was asked for.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from niharness.domain.artifacts import Answer
from niharness.domain.identity import Digest, RunId, digest_json


class Driver(StrEnum):
    """Who asked. Recorded so a run's history says who steered it."""

    CLI = "cli"
    TUI = "tui"
    MCP = "mcp"


class Command(BaseModel):
    """Base class for control commands: frozen and strict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Driver = Driver.CLI


class CancelRun(Command):
    """Stop the run at the next step boundary, leaving a resumable snapshot."""

    kind: Literal["cancel_run"] = "cancel_run"
    run_id: RunId
    reason: str = ""


class ApprovePlan(Command):
    """Approve a plan for execution. The digest ties the approval to one plan."""

    kind: Literal["approve_plan"] = "approve_plan"
    plan_digest: Digest


class AnswerQuestions(Command):
    """Answer the clarifying questions a step is waiting on."""

    kind: Literal["answer_questions"] = "answer_questions"
    run_id: RunId
    answers: tuple[Answer, ...] = Field(min_length=1)


ControlCommand = Annotated[
    CancelRun | ApprovePlan | AnswerQuestions,
    Field(discriminator="kind"),
]
"""Everything a driver may ask of a run."""


class ControlMessage(BaseModel):
    """One line of the control inbox: a command, when it arrived, and its identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Digest = Field(description="Content digest; a repeated submission is the same id.")
    ts: AwareDatetime
    command: ControlCommand

    @property
    def kind(self) -> str:
        """The command's discriminator, for filtering without unpacking."""
        return self.command.kind


def message_id(command: ControlCommand, ts: AwareDatetime) -> Digest:
    """Return the identity of a submitted command."""
    return digest_json({"command": command.model_dump(mode="json"), "ts": ts.isoformat()})


COMMAND_TYPES: tuple[type[Command], ...] = (CancelRun, ApprovePlan, AnswerQuestions)
"""Every command type, for schema export."""

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run state as a fold over the event stream.

Run state is never mutated in place: it is derived by replaying events. The
same stream always folds to the same snapshot, which is what makes `nih status`,
`nih resume` and the TUI agree without sharing any code.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain import events as ev
from niharness.domain.events import Event, RunOutcome
from niharness.domain.identity import Digest, RunId, StepId


class RunSnapshot(BaseModel):
    """Everything the harness needs to resume a run or render its status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    seq: int = Field(default=0, ge=0, description="Sequence of the last folded event.")
    stage: str = ""
    step_id: StepId | None = None
    outcome: RunOutcome | None = None
    cancelled: bool = False
    spec_digest: Digest | None = None
    plan_digest: Digest | None = None
    plan_approved: bool = False
    tokens_used: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    rejected_calls: int = Field(default=0, ge=0)
    commits: tuple[str, ...] = ()
    gate_runs: int = Field(default=0, ge=0)
    gate_failures: int = Field(default=0, ge=0)
    last_failure_signature: str = ""
    compactions: int = Field(default=0, ge=0)
    highest_intervention: int = Field(default=0, ge=0)
    open_questions: int = Field(default=0, ge=0)
    context_fill: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def finished(self) -> bool:
        """True when a terminal run event has been folded."""
        return self.outcome is not None


def fold(stream: Iterable[Event], run_id: RunId | None = None) -> RunSnapshot:
    """Replay `stream` into a snapshot.

    Args:
        stream: events in sequence order, as `events.jsonl` stores them.
        run_id: the run to fold; taken from the first event when omitted.

    Returns:
        The snapshot after the last event.

    Raises:
        ValueError: if the stream is empty and no run id was given, or if
            sequence numbers are not strictly increasing.
    """
    snapshot: RunSnapshot | None = None if run_id is None else RunSnapshot(run_id=run_id)
    for event in stream:
        if snapshot is None:
            snapshot = RunSnapshot(run_id=event.run_id)
        if event.seq <= snapshot.seq:
            msg = f"event sequence went backwards: {event.seq} after {snapshot.seq}"
            raise ValueError(msg)
        snapshot = _apply(snapshot, event)
    if snapshot is None:
        msg = "cannot fold an empty stream without a run id"
        raise ValueError(msg)
    return snapshot


def _apply(snapshot: RunSnapshot, event: Event) -> RunSnapshot:  # noqa: C901 - flat dispatch
    """Return `snapshot` with `event` applied."""
    update: dict[str, object] = {"seq": event.seq}
    body = event.body
    match body:
        case ev.RunStarted():
            update |= {"spec_digest": body.spec_digest, "plan_digest": body.plan_digest}
        case ev.RunFinished():
            update["outcome"] = body.outcome
        case ev.RunCancelled():
            update |= {"cancelled": True, "outcome": RunOutcome.CANCELLED}
        case ev.StepStarted():
            update |= {"stage": body.stage, "step_id": event.step_id}
        case ev.StepFinished():
            update |= {
                "tokens_used": snapshot.tokens_used + body.tokens_used,
                "step_id": None,
            }
        case ev.ToolCalled():
            update["tool_calls"] = snapshot.tool_calls + 1
        case ev.ToolRejected():
            update["rejected_calls"] = snapshot.rejected_calls + 1
        case ev.GateFinished():
            update |= {
                "gate_runs": snapshot.gate_runs + 1,
                "gate_failures": snapshot.gate_failures + (0 if body.passed else 1),
                "last_failure_signature": body.failure_signature,
            }
        case ev.VcsCommitted():
            update["commits"] = (*snapshot.commits, body.sha)
        case ev.ContextCompacted():
            update["compactions"] = snapshot.compactions + 1
        case ev.LoopIntervention():
            update["highest_intervention"] = max(snapshot.highest_intervention, int(body.level))
        case ev.QuestionsAsked():
            update["open_questions"] = snapshot.open_questions + body.count
        case ev.QuestionsAnswered():
            update["open_questions"] = max(0, snapshot.open_questions - body.count)
        case ev.PlanCreated():
            update["plan_digest"] = body.plan_digest
        case ev.PlanApproved():
            update["plan_approved"] = True
        case ev.SpecAccepted():
            update["spec_digest"] = body.spec_digest
        case ev.MetricsSampled():
            update["context_fill"] = body.context_fill
        case _:
            pass
    return snapshot.model_copy(update=update)

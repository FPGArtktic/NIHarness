# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The event catalogue.

Run state is an append-only stream of these events plus periodic snapshots.
Nothing in the harness mutates run state in place, and the TUI renders the
stream without computing anything of its own.

The domain produces *payloads*; the event sink stamps each payload with a
sequence number, a timestamp and the run it belongs to, which keeps the clock
at the edge and the domain deterministic.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from niharness.domain.identity import CallId, Digest, RunId, StepId


class Payload(BaseModel):
    """Base class for event payloads: frozen and strict."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RunOutcome(StrEnum):
    """How a run ended."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(StrEnum):
    """Why a step stopped. Mirrors the stop conditions of the tool loop."""

    FINAL_ANSWER = "final_answer"
    BUDGET_TOOL_CALLS = "budget_tool_calls"
    BUDGET_TOKENS = "budget_tokens"
    BUDGET_WALL_TIME = "budget_wall_time"
    DETECTOR_ESCALATION = "detector_escalation"
    CANCELLED = "cancelled"
    ERROR = "error"


class InterventionLevel(IntEnum):
    """The intervention ladder applied to a looping step."""

    SYNTHETIC_RESULT = 1
    REFLECTION_TURN = 2
    ROLLBACK_RESTART = 3
    STEP_FAILURE = 4


# --------------------------------------------------------------------- run scope


class RunStarted(Payload):
    """A run began on a fresh worktree, seeded only by its input artifact."""

    type: Literal["run.started"] = "run.started"
    branch: str
    profile: str
    model: str
    tools: Literal["sandbox", "local"] = Field(
        default="sandbox",
        description="Where the tool server ran. 'local' means outside the container.",
    )
    spec_digest: Digest | None = None
    plan_digest: Digest | None = None
    parent_run_id: RunId | None = None


class RunFinished(Payload):
    """A run reached a terminal state."""

    type: Literal["run.finished"] = "run.finished"
    outcome: RunOutcome
    reason: str = ""


class RunCancelled(Payload):
    """The human cancelled the run; the last snapshot allows a resume."""

    type: Literal["run.cancelled"] = "run.cancelled"
    source: Literal["tui", "cli", "signal"]


class RunResumed(Payload):
    """A run continued from a snapshot."""

    type: Literal["run.resumed"] = "run.resumed"
    from_seq: int = Field(ge=1)


# -------------------------------------------------------------------- step scope


class StepStarted(Payload):
    """A pipeline step began with a budget the harness enforces."""

    type: Literal["step.started"] = "step.started"
    stage: str
    role: str
    task_id: str | None = None
    max_tool_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)
    max_wall_seconds: int = Field(ge=0)


class StepFinished(Payload):
    """A step stopped; counters are what the harness measured, not what the model claimed."""

    type: Literal["step.finished"] = "step.finished"
    stop_reason: StopReason
    tool_calls_used: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    wall_seconds: float = Field(ge=0)


class StepFailed(Payload):
    """A step failed and the task goes back to the human."""

    type: Literal["step.failed"] = "step.failed"
    reason: str
    diagnostics: str = ""


# ------------------------------------------------------------------- model scope


class ModelRequest(Payload):
    """A request was sent to the inference backend."""

    type: Literal["model.request"] = "model.request"
    model: str
    messages_digest: Digest
    prompt_tokens: int = Field(ge=0)
    window_fill: float = Field(ge=0, le=1)
    tool_choice: Literal["auto", "none", "required"] = "auto"


class ModelResponse(Payload):
    """A response arrived. Token counts come from the backend, never from characters."""

    type: Literal["model.response"] = "model.response"
    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    finish_reason: str
    content_digest: Digest


class ModelReasoning(Payload):
    """Reasoning content, logged for the human and stripped from the message history."""

    type: Literal["model.reasoning"] = "model.reasoning"
    text: str


# -------------------------------------------------------------------- tool scope


class ToolCalled(Payload):
    """A validated tool call was dispatched to the sandbox."""

    type: Literal["tool.called"] = "tool.called"
    call_id: CallId
    name: str
    arguments: dict[str, Any]
    arguments_digest: Digest


class ToolReturned(Payload):
    """A tool call produced a structured, truncated result."""

    type: Literal["tool.returned"] = "tool.returned"
    call_id: CallId
    ok: bool
    result_digest: Digest
    truncated: bool
    summary: str


class ToolRejected(Payload):
    """A tool call was refused before execution: schema, allowlist or path confinement."""

    type: Literal["tool.rejected"] = "tool.rejected"
    name: str
    violation: str
    detail: str


# -------------------------------------------------------------------- gate scope


class GateStarted(Payload):
    """A gate tier began; its command list is fixed up front."""

    type: Literal["gate.started"] = "gate.started"
    tier: Literal["quick", "full"]
    commands: tuple[str, ...]


class GateCommandFinished(Payload):
    """One gate command finished."""

    type: Literal["gate.command"] = "gate.command"
    command: str
    exit_code: int
    duration_seconds: float = Field(ge=0)
    output_tail: str = ""


class GateFinished(Payload):
    """A gate tier finished. Red means no commit."""

    type: Literal["gate.finished"] = "gate.finished"
    tier: Literal["quick", "full"]
    passed: bool
    failure_signature: str = ""


# ----------------------------------------------------------------- context scope


class ContextCompacted(Payload):
    """The message window was compacted. The event log itself is untouched."""

    type: Literal["context.compacted"] = "context.compacted"
    before_tokens: int = Field(ge=0)
    after_tokens: int = Field(ge=0)
    deterministic_only: bool
    summary_digest: Digest | None = None
    indexed_chunks: int = Field(default=0, ge=0)


# -------------------------------------------------------------------- loop scope


class LoopDetected(Payload):
    """A detector fired over the step's event window."""

    type: Literal["loop.detected"] = "loop.detected"
    pathology: str
    observed: int = Field(ge=0)
    threshold: int = Field(ge=0)
    evidence: str = ""


class LoopIntervention(Payload):
    """The harness intervened in a looping step."""

    type: Literal["loop.intervention"] = "loop.intervention"
    level: InterventionLevel
    pathology: str


# --------------------------------------------------------------- artifact scope


class SpecAccepted(Payload):
    """The spec passed the schema check and the reviewer pass."""

    type: Literal["spec.accepted"] = "spec.accepted"
    spec_digest: Digest


class PlanCreated(Payload):
    """A plan was produced or imported and passed the plan gate."""

    type: Literal["plan.created"] = "plan.created"
    plan_digest: Digest
    task_count: int = Field(ge=1)
    imported: bool = False


class PlanApproved(Payload):
    """The human approved the plan; implementation starts as a fresh run."""

    type: Literal["plan.approved"] = "plan.approved"
    plan_digest: Digest


class QuestionsAsked(Payload):
    """A step emitted clarifying questions and is waiting for the human."""

    type: Literal["questions.asked"] = "questions.asked"
    questions_digest: Digest
    count: int = Field(ge=1, le=3)


class QuestionsAnswered(Payload):
    """The human answered; answers are events, so a replay sees them."""

    type: Literal["questions.answered"] = "questions.answered"
    answers_digest: Digest
    count: int = Field(ge=1)


class NotesAppended(Payload):
    """Something was written to NOTES.md: an uncertainty, a blocked dependency, a diagnosis."""

    type: Literal["notes.appended"] = "notes.appended"
    section: str
    text_digest: Digest


# ------------------------------------------------------------------- vcs scope


class VcsCommitted(Payload):
    """The gate passed and a commit landed on the run branch."""

    type: Literal["vcs.committed"] = "vcs.committed"
    sha: str
    subject: str
    files_changed: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class VcsRolledBack(Payload):
    """The worktree was reset to the last green commit."""

    type: Literal["vcs.rolled_back"] = "vcs.rolled_back"
    to_sha: str
    reason: str


# --------------------------------------------------------------- metrics scope


class MetricsSampled(Payload):
    """A metrics sample for the TUI gauges. The harness computes, the TUI renders."""

    type: Literal["metrics.sampled"] = "metrics.sampled"
    generation_tps: float = Field(ge=0)
    prompt_tps: float = Field(ge=0)
    context_fill: float = Field(ge=0, le=1)
    vram_used_mb: int = Field(ge=0)
    ram_used_mb: int = Field(ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)


EventBody = Annotated[
    RunStarted
    | RunFinished
    | RunCancelled
    | RunResumed
    | StepStarted
    | StepFinished
    | StepFailed
    | ModelRequest
    | ModelResponse
    | ModelReasoning
    | ToolCalled
    | ToolReturned
    | ToolRejected
    | GateStarted
    | GateCommandFinished
    | GateFinished
    | ContextCompacted
    | LoopDetected
    | LoopIntervention
    | SpecAccepted
    | PlanCreated
    | PlanApproved
    | QuestionsAsked
    | QuestionsAnswered
    | NotesAppended
    | VcsCommitted
    | VcsRolledBack
    | MetricsSampled,
    Field(discriminator="type"),
]
"""Discriminated union of every event payload the harness emits."""


class Event(BaseModel):
    """One line of `events.jsonl`: an envelope around a payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1, description="Position in the run's stream, starting at 1.")
    ts: AwareDatetime = Field(description="When the sink accepted the payload (UTC).")
    run_id: RunId
    step_id: StepId | None = None
    body: EventBody

    @property
    def type(self) -> str:
        """Return the payload's discriminator, for filtering without unpacking."""
        return self.body.type


PAYLOAD_TYPES: tuple[type[Payload], ...] = (
    RunStarted,
    RunFinished,
    RunCancelled,
    RunResumed,
    StepStarted,
    StepFinished,
    StepFailed,
    ModelRequest,
    ModelResponse,
    ModelReasoning,
    ToolCalled,
    ToolReturned,
    ToolRejected,
    GateStarted,
    GateCommandFinished,
    GateFinished,
    ContextCompacted,
    LoopDetected,
    LoopIntervention,
    SpecAccepted,
    PlanCreated,
    PlanApproved,
    QuestionsAsked,
    QuestionsAnswered,
    NotesAppended,
    VcsCommitted,
    VcsRolledBack,
    MetricsSampled,
)
"""Every payload type, in catalogue order; `nih docs generate` renders this."""

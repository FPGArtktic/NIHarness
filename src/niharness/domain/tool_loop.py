# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool loop, as a pure state machine.

A step that uses tools is a bounded ReAct loop. The loop itself cannot call a
model, run a container or read a clock, so it is written the only way that
leaves it testable: it **decides**, and an executor **acts**.

    action = next_action(state)        # what should happen now
    ...                                # the executor performs it
    step = observe_model(state, ...)   # what came back
    state, events = step.state, step.events

Every transition also returns the events to append, so the executor never has
to reconstruct what happened - it appends what it is handed. The same inputs
always produce the same decisions and the same events, which is what makes a
run replayable and the detectors testable on synthetic windows.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from niharness.domain import events as ev
from niharness.domain.artifacts import FinalAnswer, Verdict
from niharness.domain.budgets import Budgets, BudgetState
from niharness.domain.events import EventBody, InterventionLevel, StopReason
from niharness.domain.identity import CallId, Digest, digest_text
from niharness.domain.loop import (
    REFLECTION_INSTRUCTION,
    CallRecord,
    LoopThresholds,
    degenerate_output,
    evaluate,
    next_level,
    synthetic_result,
)
from niharness.domain.messages import ChatResponse, ResponseFormat, ToolChoice
from niharness.domain.roles import Role
from niharness.domain.tools import (
    EMPTY_CATALOG,
    AcceptedCall,
    RejectedCall,
    ToolCall,
    ToolCatalog,
    ToolId,
    ToolLimits,
    ToolResult,
    validate_call,
)

FINALIZE_INSTRUCTION = (
    "The tool budget for this step is nearly spent, so no further tool call will be "
    "accepted. Write the current state: what is done, what is not, what you would do "
    "next, and anything the next step must know. Put blockers in NOTES.md."
)
"""What the model is told when the harness forces finalisation."""

MAX_INVALID_RESPONSES = 3
"""How often a malformed final answer may be re-requested before the step fails."""

FinalArtifact = FinalAnswer | Verdict
"""What a step may end with: work reported done, or a review's verdict."""


class ArtifactKind(StrEnum):
    """Which artifact ends a step. The role decides; the loop enforces."""

    FINAL_ANSWER = "FinalAnswer"
    VERDICT = "Verdict"


ARTIFACT_MODELS: dict[ArtifactKind, type[FinalAnswer] | type[Verdict]] = {
    ArtifactKind.FINAL_ANSWER: FinalAnswer,
    ArtifactKind.VERDICT: Verdict,
}
"""The model each kind validates against."""


class LoopConfig(BaseModel):
    """Everything a step's loop needs to decide, and nothing it needs to act."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    role: Role
    model: str
    allowlist: frozenset[ToolId]
    catalog: ToolCatalog = EMPTY_CATALOG
    """External tools this step may reach; empty unless a server is configured."""
    budgets: Budgets = Budgets()
    thresholds: LoopThresholds = LoopThresholds()
    limits: ToolLimits = ToolLimits()
    final_artifact: ArtifactKind | None = Field(
        default=ArtifactKind.FINAL_ANSWER,
        description="The artifact that ends this step; None means any prose answer does.",
    )
    task_id: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4_096, ge=1)
    response_format: ResponseFormat | None = Field(
        default=None,
        description="Structured output constraint for the role's artifact, when it has one.",
    )
    seed: int | None = Field(default=None, description="Determinism knob, passed through.")


class LoopState(BaseModel):
    """The loop's whole memory. Immutable: every transition returns a new one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: LoopConfig
    budget: BudgetState
    calls: tuple[CallRecord, ...] = ()
    interventions: tuple[InterventionLevel, ...] = ()
    pending_call: AcceptedCall | None = None
    pending_instruction: str = ""
    pending_tool_choice: ToolChoice = ToolChoice.AUTO
    synthetic_for: CallId | None = None
    synthetic_text: str = ""
    rollback_reason: str = ""
    invalid_responses: int = Field(default=0, ge=0)
    results_by_arguments: dict[str, str] = Field(default_factory=dict)
    stop_reason: StopReason | None = None
    final: FinalArtifact | None = None

    @property
    def finished(self) -> bool:
        """True when the step has stopped."""
        return self.stop_reason is not None

    @property
    def final_answer(self) -> FinalAnswer | None:
        """The step's FinalAnswer, when that is what ended it."""
        return self.final if isinstance(self.final, FinalAnswer) else None

    @property
    def verdict(self) -> Verdict | None:
        """The step's Verdict, when that is what ended it."""
        return self.final if isinstance(self.final, Verdict) else None

    @property
    def next_seq(self) -> int:
        """Position of the next call record in this step."""
        return len(self.calls) + 1


# ------------------------------------------------------------------- actions


class CallModel(BaseModel):
    """Ask the model for the next move."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["call_model"] = "call_model"
    tool_choice: ToolChoice = ToolChoice.AUTO
    instruction: str = Field(
        default="",
        description="Appended to the window: a rejection, a reflection, a finalisation.",
    )


class ExecuteTool(BaseModel):
    """Run a validated call in the sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["execute_tool"] = "execute_tool"
    call: AcceptedCall


class InjectResult(BaseModel):
    """Answer a repeated call from the harness, with no model and no execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inject_result"] = "inject_result"
    call_id: CallId
    text: str


class Rollback(BaseModel):
    """Reset the worktree to the last green commit and restart the step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["rollback"] = "rollback"
    reason: str


class Stop(BaseModel):
    """The step is over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stop"] = "stop"
    stop_reason: StopReason
    final: FinalArtifact | None = None


Action = Annotated[
    CallModel | ExecuteTool | InjectResult | Rollback | Stop,
    Field(discriminator="kind"),
]
"""What the executor should do next."""


class Step(BaseModel):
    """A transition: the new state, and the events to append for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: LoopState
    events: tuple[EventBody, ...] = ()


# ----------------------------------------------------------------- lifecycle


def start(config: LoopConfig) -> Step:
    """Begin a step.

    Args:
        config: the step's role, budgets, allowlist and thresholds.

    Returns:
        The initial state and the `step.started` event.
    """
    state = LoopState(config=config, budget=BudgetState(budgets=config.budgets))
    started = ev.StepStarted(
        stage=config.stage,
        role=config.role.value,
        task_id=config.task_id,
        max_tool_calls=config.budgets.max_tool_calls,
        max_tokens=config.budgets.max_tokens,
        max_wall_seconds=config.budgets.max_wall_seconds,
    )
    return Step(state=state, events=(started,))


def next_action(state: LoopState) -> Action:
    """Return what should happen now.

    The order of the checks is the order of the stop conditions: a finished
    step stays finished, an escalation outranks a pending call, and a nearly
    spent tool budget forces a finalising turn rather than another call.
    """
    if state.stop_reason is not None:
        return Stop(stop_reason=state.stop_reason, final=state.final)
    if state.rollback_reason:
        return Rollback(reason=state.rollback_reason)
    if state.synthetic_for is not None:
        return InjectResult(call_id=state.synthetic_for, text=state.synthetic_text)
    if state.pending_call is not None:
        return ExecuteTool(call=state.pending_call)
    if state.budget.must_finalize:
        return CallModel(
            tool_choice=ToolChoice.NONE,
            instruction=state.pending_instruction or FINALIZE_INSTRUCTION,
        )
    return CallModel(
        tool_choice=state.pending_tool_choice,
        instruction=state.pending_instruction,
    )


def _spent(state: LoopState, **amounts: float) -> LoopState:
    """Return `state` with the given budget amounts spent, stopping if exhausted."""
    budget = state.budget.spend(
        tool_calls=int(amounts.get("tool_calls", 0)),
        tokens=int(amounts.get("tokens", 0)),
        wall_seconds=amounts.get("wall_seconds", 0.0),
    )
    return state.model_copy(update={"budget": budget})


def _finish(state: LoopState, reason: StopReason) -> Step:
    """Stop the step and emit `step.finished`."""
    stopped = state.model_copy(update={"stop_reason": reason})
    finished = ev.StepFinished(
        stop_reason=reason,
        tool_calls_used=stopped.budget.tool_calls_used,
        tokens_used=stopped.budget.tokens_used,
        wall_seconds=stopped.budget.wall_seconds_used,
    )
    return Step(state=stopped, events=(finished,))


def _record(state: LoopState, record: CallRecord) -> LoopState:
    """Append a call record to the detector window."""
    return state.model_copy(update={"calls": (*state.calls, record)})


def _intervene(state: LoopState) -> tuple[LoopState, tuple[EventBody, ...]]:
    """Run the detectors and apply the next rung of the ladder, if any fired.

    Returns:
        The state after the intervention and the events that describe it.
    """
    findings = evaluate(state.calls, state.config.thresholds)
    if not findings:
        return state, ()
    level = next_level(state.interventions, findings)
    if level is None:  # pragma: no cover - findings imply a level
        return state, ()
    leading = findings[0]
    emitted: list[EventBody] = [
        ev.LoopDetected(
            pathology=finding.pathology.value,
            observed=finding.observed,
            threshold=finding.threshold,
            evidence=finding.evidence,
        )
        for finding in findings
    ]
    emitted.append(ev.LoopIntervention(level=level, pathology=leading.pathology.value))
    updated = state.model_copy(
        update={"interventions": (*state.interventions, level)},
    )
    if level is InterventionLevel.REFLECTION_TURN:
        updated = updated.model_copy(
            update={
                "pending_instruction": REFLECTION_INSTRUCTION,
                "pending_tool_choice": ToolChoice.NONE,
            },
        )
    elif level is InterventionLevel.ROLLBACK_RESTART:
        updated = updated.model_copy(
            update={"rollback_reason": f"{leading.pathology.value}: {leading.evidence}"},
        )
    return updated, tuple(emitted)


def observe_model(state: LoopState, response: ChatResponse) -> Step:
    """Fold a model response into the loop.

    One tool call is taken per turn, deliberately: a step that fans out loses
    the ability to react to a result before deciding the next move, and the
    detectors lose the ordering they reason about.

    Args:
        state: the current state.
        response: what the backend returned.

    Returns:
        The new state and the events for this turn.
    """
    if state.finished:
        return Step(state=state)

    emitted: list[EventBody] = [
        ev.ModelResponse(
            model=state.config.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            finish_reason=response.finish_reason,
            content_digest=digest_text(response.content),
        ),
    ]
    if response.reasoning_content:
        emitted.append(ev.ModelReasoning(text=response.reasoning_content))

    # What the model *wrote*, not what it was shown. The prompt is re-sent
    # every turn, so counting it would shrink the budget as the conversation
    # grows - punishing a step for having context, and exhausting the budget on
    # reading rather than on work. Measured on a live run: a 27B spent its
    # whole token budget on four file reads. The window is bounded by the
    # context window and compaction, and the compute is bounded by the clock.
    working = _spent(state, tokens=response.usage.completion_tokens)
    working = working.model_copy(
        update={"pending_instruction": "", "pending_tool_choice": ToolChoice.AUTO},
    )

    degenerate = degenerate_output(response.content, state.config.thresholds)
    if degenerate is not None:
        emitted.append(
            ev.LoopDetected(
                pathology=degenerate.pathology.value,
                observed=degenerate.observed,
                threshold=degenerate.threshold,
                evidence=degenerate.evidence,
            ),
        )

    if response.tool_calls:
        working, call_events = _handle_tool_call(working, response)
        emitted.extend(call_events)
    else:
        working, answer_events = _handle_final_answer(working, response)
        emitted.extend(answer_events)

    if working.stop_reason is not None:
        return Step(state=working, events=tuple(emitted))
    exhausted = working.budget.exhausted()
    if exhausted is not None and working.pending_call is None:
        closing = _finish(working, exhausted)
        return Step(state=closing.state, events=(*emitted, *closing.events))
    return Step(state=working, events=tuple(emitted))


def _handle_tool_call(
    state: LoopState,
    response: ChatResponse,
) -> tuple[LoopState, tuple[EventBody, ...]]:
    """Validate the proposed call, or turn it into a rejection or an intervention."""
    proposal = response.tool_calls[0]
    call = ToolCall(
        call_id=proposal.call_id,
        name=proposal.name,
        arguments=_decode_arguments(proposal.arguments_json),
    )
    validated = validate_call(
        call,
        state.config.allowlist,
        state.config.limits,
        state.config.catalog,
    )
    if isinstance(validated, RejectedCall):
        return _handle_rejection(state, call, validated)

    # Digest the *validated* arguments, not the proposal: two calls that differ
    # only in which defaults they spell out are the same call, and the repeat
    # detector has to see them that way.
    digest = _arguments_digest_of(validated)
    key = f"{validated.tool}:{digest}"
    previous = state.results_by_arguments.get(key)
    if previous is not None:
        text = synthetic_result(validated.tool, previous)
        repeated = state.model_copy(
            update={"synthetic_for": call.call_id, "synthetic_text": text},
        )
        repeated = _record(
            repeated,
            CallRecord(
                seq=repeated.next_seq,
                tool=validated.tool,
                arguments_digest=digest,
                result_digest=Digest(previous),
            ),
        )
        after, events = _intervene(repeated)
        # The synthetic answer is rung one. Once the ladder has climbed past it,
        # the rung itself is the intervention - answering the call again would
        # let the step keep repeating while the ladder quietly escalated.
        applied = after.interventions[-1] if after.interventions else None
        if applied is not None and applied is not InterventionLevel.SYNTHETIC_RESULT:
            after = after.model_copy(update={"synthetic_for": None, "synthetic_text": ""})
        return _escalate_if_final(after), events

    accepted = state.model_copy(update={"pending_call": validated})
    called = ev.ToolCalled(
        call_id=call.call_id,
        name=validated.tool,
        arguments=call.arguments,
        arguments_digest=digest,
    )
    return accepted, (called,)


def _handle_rejection(
    state: LoopState,
    call: ToolCall,
    rejection: RejectedCall,
) -> tuple[LoopState, tuple[EventBody, ...]]:
    """Record a refused call and ask the model again, bounded by the detectors."""
    rejected = ev.ToolRejected(
        name=call.name,
        violation=rejection.violation.value,
        detail=rejection.detail,
    )
    recorded = _record(
        state,
        CallRecord(
            seq=state.next_seq,
            tool=call.name,
            arguments_digest=call.arguments_digest(),
            invalid=True,
        ),
    )
    instructed = recorded.model_copy(
        update={
            "pending_instruction": (
                f"The call to {call.name!r} was refused before it ran "
                f"({rejection.violation.value}): {rejection.detail}. "
                "Correct it or choose a different action."
            ),
        },
    )
    after, events = _intervene(instructed)
    return _escalate_if_final(after), (rejected, *events)


def _handle_final_answer(
    state: LoopState,
    response: ChatResponse,
) -> tuple[LoopState, tuple[EventBody, ...]]:
    """Accept the step's artifact, or ask once more when it does not validate."""
    kind = state.config.final_artifact
    if kind is None:
        stopped = _finish(state, StopReason.FINAL_ANSWER)
        return stopped.state, stopped.events
    try:
        artifact = ARTIFACT_MODELS[kind].model_validate_json(response.content)
    except (ValidationError, ValueError) as error:
        attempts = state.invalid_responses + 1
        retrying = state.model_copy(
            update={
                "invalid_responses": attempts,
                "pending_instruction": (
                    f"The answer did not validate against the {kind.value} schema: "
                    f"{error}. Answer again, and only with that artifact."
                ),
            },
        )
        if attempts >= MAX_INVALID_RESPONSES:
            failed = _finish(retrying, StopReason.ERROR)
            return failed.state, failed.events
        return retrying, ()
    answered = state.model_copy(update={"final": artifact})
    stopped = _finish(answered, StopReason.FINAL_ANSWER)
    return stopped.state, stopped.events


def observe_tool(
    state: LoopState,
    result: ToolResult,
    *,
    tree_digest: Digest | None = None,
    gate_signature: str = "",
    wall_seconds: float = 0.0,
) -> Step:
    """Fold a tool result into the loop.

    Args:
        state: the current state, with a pending call.
        result: the truncated, structured result.
        tree_digest: the worktree digest after the call, for the cycle detector.
        gate_signature: the normalised gate failure, when the tool was a gate.
        wall_seconds: how long the call took.

    Returns:
        The new state and the events for this result.
    """
    if state.finished or state.pending_call is None:
        return Step(state=state)

    pending = state.pending_call
    digest = Digest(str(result.result_digest()))
    returned = ev.ToolReturned(
        call_id=result.call_id,
        ok=result.ok,
        result_digest=digest,
        truncated=result.truncated,
        summary=result.summary,
    )
    arguments_digest = _arguments_digest_of(pending)
    working = _spent(state, tool_calls=1, wall_seconds=wall_seconds)
    working = working.model_copy(
        update={
            "pending_call": None,
            "results_by_arguments": {
                **state.results_by_arguments,
                f"{pending.tool}:{arguments_digest}": str(digest),
            },
        },
    )
    working = _record(
        working,
        CallRecord(
            seq=working.next_seq,
            tool=pending.tool,
            arguments_digest=arguments_digest,
            result_digest=digest,
            tree_digest=tree_digest,
            gate_signature=gate_signature,
        ),
    )
    working, intervention_events = _intervene(working)
    working = _escalate_if_final(working)
    if working.stop_reason is not None:
        closing = _finish(working, StopReason.DETECTOR_ESCALATION)
        return Step(
            state=closing.state,
            events=(returned, *intervention_events, *closing.events),
        )
    exhausted = working.budget.exhausted()
    if exhausted is not None:
        closing = _finish(working, exhausted)
        return Step(
            state=closing.state,
            events=(returned, *intervention_events, *closing.events),
        )
    return Step(state=working, events=(returned, *intervention_events))


def restarted(state: LoopState) -> Step:
    """Return the state after the executor performed a rollback.

    The detector window is cleared, because the attempts it described are gone
    with the rolled-back changes, but the intervention ladder is not: a step
    that loops again after a restart escalates rather than starting over.
    """
    cleared = state.model_copy(
        update={
            "calls": (),
            "results_by_arguments": {},
            "rollback_reason": "",
            "pending_call": None,
            "synthetic_for": None,
            "synthetic_text": "",
            "pending_instruction": (
                "The step was rolled back to the last green commit after repeating "
                "itself. The previous attempts are summarised in the window; do not "
                "repeat them."
            ),
            "pending_tool_choice": ToolChoice.AUTO,
        },
    )
    return Step(state=cleared)


def injected(state: LoopState) -> Step:
    """Return the state after the executor delivered a synthetic result."""
    return Step(
        state=state.model_copy(update={"synthetic_for": None, "synthetic_text": ""}),
    )


def cancel(state: LoopState) -> Step:
    """Stop the step because the run was cancelled."""
    if state.finished:
        return Step(state=state)
    return _finish(state, StopReason.CANCELLED)


def _escalate_if_final(state: LoopState) -> LoopState:
    """Mark the step failed when the ladder reached its last rung."""
    if state.interventions and state.interventions[-1] is InterventionLevel.STEP_FAILURE:
        return state.model_copy(update={"stop_reason": StopReason.DETECTOR_ESCALATION})
    return state


def _arguments_digest_of(call: AcceptedCall) -> Digest:
    """Return the digest of a pending call's arguments."""
    return ToolCall(
        call_id=call.call_id,
        name=call.tool,
        arguments=call.args.model_dump(mode="json"),
    ).arguments_digest()


def _decode_arguments(payload: str) -> dict[str, object]:
    """Decode a tool call's arguments, treating malformed JSON as empty.

    An unparsable argument object is not special-cased here: it fails schema
    validation like any other bad call, which keeps one rejection path.
    """
    try:
        decoded = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def window(state: LoopState) -> Sequence[CallRecord]:
    """Return the detector window, for diagnostics and tests."""
    return state.calls

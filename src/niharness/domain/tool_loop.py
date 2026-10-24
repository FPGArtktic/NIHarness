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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from niharness.domain import events as ev
from niharness.domain.artifacts import FinalAnswer, PlanDraft, Spec, SpecVerdict, Verdict
from niharness.domain.budgets import Budgets, BudgetState
from niharness.domain.events import EventBody, InterventionLevel, StopReason
from niharness.domain.identity import CallId, Digest, digest_text
from niharness.domain.loop import (
    REFLECTION_INSTRUCTION,
    CallRecord,
    LoopFinding,
    LoopThresholds,
    degenerate_output,
    evaluate,
    next_level,
    synthetic_result,
    unbacked_claims,
    uncommitted_completion,
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

FinalArtifact = FinalAnswer | Verdict | PlanDraft | SpecVerdict | Spec
"""What a step may end with: work reported done, a verdict on a commit or on a
spec, a plan, or a spec itself."""


class ArtifactKind(StrEnum):
    """Which artifact ends a step. The role decides; the loop enforces."""

    FINAL_ANSWER = "FinalAnswer"
    VERDICT = "Verdict"
    PLAN_DRAFT = "PlanDraft"
    SPEC_VERDICT = "SpecVerdict"
    SPEC = "Spec"


REQUIRES_COMMIT_DEFAULT = False
"""Whether a step owes a commit. Declared by the caller, never inferred.

The pipeline's implement and fix steps owe one: §4 and the Definition of Done
both say the change lands in a commit through the gate. An arbitrary
tool-using step does not - a benchmark task that edits a file to prove it can
call `replace_in_file` has finished when the file is edited, and demanding a
commit of it would measure a pipeline nobody runs.
"""

DEFAULT_ARTIFACT = ArtifactKind.FINAL_ANSWER
"""What ends a step unless the caller says otherwise."""


ARTIFACT_MODELS: dict[ArtifactKind, type[FinalArtifact]] = {
    ArtifactKind.FINAL_ANSWER: FinalAnswer,
    ArtifactKind.VERDICT: Verdict,
    ArtifactKind.PLAN_DRAFT: PlanDraft,
    ArtifactKind.SPEC_VERDICT: SpecVerdict,
    ArtifactKind.SPEC: Spec,
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
        default=DEFAULT_ARTIFACT,
        description="The artifact that ends this step; None means any prose answer does.",
    )
    requires_commit: bool = Field(
        default=REQUIRES_COMMIT_DEFAULT,
        description="Whether reporting this step done also claims a commit. True for "
        "the pipeline's implement and fix steps; false for a step that owes no commit, "
        "which is why it is declared rather than inferred from the allowlist.",
    )
    task_id: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4_096, ge=1)
    response_format: ResponseFormat | None = Field(
        default=None,
        description="Structured output constraint for the role's artifact, when it has one.",
    )
    seed: int | None = Field(default=None, description="Determinism knob, passed through.")

    @model_validator(mode="before")
    @classmethod
    def _artifact_brings_its_schema(cls, data: Any) -> Any:  # noqa: ANN401 - pydantic's raw input
        """Give a step that demands an artifact the schema for it, if none was passed.

        The two defaults disagreed: `final_artifact` is `final_answer` while
        `response_format` was None, so a caller setting neither got a step that
        demanded an artifact whose schema was never sent - and the loop then
        rejected every prose answer until the step errored. Found by building
        exactly that config for `nih bench`, where eight tasks ran to
        `StopReason.ERROR` before the cause was obvious.

        Derived rather than refused, because the schema is not a choice: it is
        the artifact's own and there is exactly one right value to fill in. A
        caller wanting a different name, or a GBNF grammar beside it, still
        passes its own and this leaves it alone.
        """
        if not isinstance(data, dict) or data.get("response_format") is not None:
            return data
        raw = data.get("final_artifact", DEFAULT_ARTIFACT)
        if raw is None:
            return data
        kind = ArtifactKind(raw)
        return {
            **data,
            "response_format": ResponseFormat(
                name=kind.value,
                json_schema=ARTIFACT_MODELS[kind].model_json_schema(),
            ),
        }


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
    reflecting: bool = Field(
        default=False,
        description="Whether the next turn is the reflection turn. It withholds tools "
        "and asks for prose, so it must neither carry the artifact's grammar nor have "
        "its answer read as the step's final one.",
    )
    commit_asked: bool = Field(
        default=False,
        description="Whether a completed step has already been told it has not "
        "committed. Asked once: a step that cannot end is worse than a claim the "
        "log records.",
    )
    evidence_asked: bool = Field(
        default=False,
        description="Whether a verdict has already been sent back for unearned "
        "evidence. Asked once: a second refusal would be a step that cannot end.",
    )
    results_by_arguments: dict[str, str] = Field(default_factory=dict)
    failures_by_arguments: frozenset[str] = Field(
        default_factory=frozenset,
        description="Which memoised results were refusals, so a repeat can quote them.",
    )
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
    def plan_draft(self) -> PlanDraft | None:
        """The step's PlanDraft, when that is what ended it."""
        return self.final if isinstance(self.final, PlanDraft) else None

    @property
    def spec_verdict(self) -> SpecVerdict | None:
        """The step's SpecVerdict, when that is what ended it."""
        return self.final if isinstance(self.final, SpecVerdict) else None

    @property
    def spec(self) -> Spec | None:
        """The step's Spec, when that is what ended it."""
        return self.final if isinstance(self.final, Spec) else None

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
    expects_artifact: bool = Field(
        default=False,
        description="Whether this turn's grammar should demand the step's artifact. "
        "Withholding tools is not the same question: a reflection turn withholds them "
        "and asks for prose, and attaching the schema to it asks the model for two "
        "incompatible things at once.",
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
            expects_artifact=True,
        )
    return CallModel(
        tool_choice=state.pending_tool_choice,
        instruction=state.pending_instruction,
        # A turn with no tools is a turn that must answer *something*, and the
        # artifact is what a step answers with - except the reflection turn,
        # which asks for prose and gets a grammar it cannot satisfy otherwise.
        expects_artifact=state.pending_tool_choice is ToolChoice.NONE and not state.reflecting,
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
                "reflecting": True,
            },
        )
    elif level is InterventionLevel.ROLLBACK_RESTART:
        updated = updated.model_copy(
            update={"rollback_reason": f"{leading.pathology.value}: {leading.evidence}"},
        )
    return updated, tuple(emitted)


def observe_model(state: LoopState, response: ChatResponse, *, seconds: float = 0.0) -> Step:
    """Fold a model response into the loop.

    One tool call is taken per turn, deliberately: a step that fans out loses
    the ability to react to a result before deciding the next move, and the
    detectors lose the ordering they reason about.

    Args:
        state: the current state.
        response: what the backend returned.
        seconds: how long the call took. Passed in rather than measured here,
            because the domain does not read a clock - and charged against the
            wall budget, because inference is where a step's time goes.

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
            duration_seconds=seconds,
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
    working = _spent(state, tokens=response.usage.completion_tokens, wall_seconds=seconds)
    working = working.model_copy(
        update={"pending_instruction": "", "pending_tool_choice": ToolChoice.AUTO},
    )
    reflected = state.reflecting
    if reflected:
        working = working.model_copy(update={"reflecting": False})

    degenerate = degenerate_response(response, state.config.thresholds)
    if degenerate is not None:
        emitted.append(
            ev.LoopDetected(
                pathology=degenerate.pathology.value,
                observed=degenerate.observed,
                threshold=degenerate.threshold,
                evidence=degenerate.evidence,
            ),
        )
        refused, refusal_events = _handle_degenerate(working, degenerate)
        emitted.extend(refusal_events)
        return Step(state=refused, events=tuple(emitted))

    if response.tool_calls:
        # A call proposed on the reflection turn is still a call the detectors
        # should see. The instruction said none would be accepted, and a model
        # that answers it by re-sending the same one has earned the next rung.
        working, call_events = _handle_tool_call(working, response)
        emitted.extend(call_events)
    elif reflected:
        # The reflection turn asked for prose, so its answer is not the step's
        # answer. Reading it as one ended the step on a checklist the model was
        # never filling: measured on L3, where every task a detector touched
        # came back handed over with the reflection's own words as its unticked
        # Definition of Done. The rung meant to get a model unstuck was
        # terminating its task instead.
        pass
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
        text = synthetic_result(
            validated.tool,
            previous,
            failed=key in state.failures_by_arguments,
        )
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


def degenerate_response(
    response: ChatResponse,
    thresholds: LoopThresholds,
) -> LoopFinding | None:
    """Look for a runaway repetition in the prose *and* in the tool arguments.

    Checking `content` alone misses the shape this actually takes. Observed on
    a live run: a 4B emitted `last_retry_time_ms_ns_ns_ns...` growing inside a
    `replace_in_file` argument until the tool call's JSON was truncated
    mid-string, and the backend then returned 500 when the harness asked it to
    render that history. The prose was empty, so the detector saw nothing.

    Public because the executor has to ask the same question *before* it adds
    the turn to the window. Refusing the response is not enough on its own:
    the window had already recorded it, and the next tokenizer call failed on
    the junk rather than on anything the loop could still decide.
    """
    found = degenerate_output(response.content, thresholds)
    if found is not None:
        return found
    for call in response.tool_calls:
        found = degenerate_output(call.arguments_json, thresholds)
        if found is not None:
            return found
    return None


def _handle_degenerate(
    state: LoopState,
    finding: LoopFinding,
) -> tuple[LoopState, tuple[EventBody, ...]]:
    """Refuse a degenerate response instead of folding it into the window.

    The rule said "abort generation" and the code only logged the finding,
    then went on to dispatch the call. Nothing downstream survives that: the
    arguments are truncated mid-string, the window carries the junk, and the
    next tokenizer call fails on it - which ended a benchmark run rather than a
    step.

    So it is treated as a refused call: recorded invalid, the model told what
    happened, and the ladder bounds it. A model that keeps producing this ends
    the step through the detectors rather than through an HTTP 500.
    """
    recorded = _record(
        state,
        CallRecord(
            seq=state.next_seq,
            tool="(response)",
            arguments_digest=digest_text(finding.evidence),
            invalid=True,
        ),
    )
    instructed = recorded.model_copy(
        update={
            "pending_instruction": (
                "That answer repeated itself and was discarded without being run "
                f"({finding.observed} repetitions of the same phrase). Answer again, "
                "shorter, and do not repeat a line you have already written."
            ),
        },
    )
    after, events = _intervene(instructed)
    return _escalate_if_final(after), events


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
                # Ask again on a turn that cannot call a tool, which is the
                # turn the artifact's schema is attached to. Retrying with
                # tools still offered asks for the artifact without
                # constraining the answer to it, and a model that has just
                # answered in prose will do it again: observed on a live run,
                # where a 27B answered, failed to validate, and was asked the
                # same unconstrained question until the step errored.
                "pending_tool_choice": ToolChoice.NONE,
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
    if isinstance(artifact, Verdict):
        asked = _ask_for_the_evidence(state, artifact)
        if asked is not None:
            return asked
    if isinstance(artifact, FinalAnswer):
        asked = _ask_for_the_commit(state, artifact)
        if asked is not None:
            return asked
    answered = state.model_copy(update={"final": artifact})
    stopped = _finish(answered, StopReason.FINAL_ANSWER)
    return stopped.state, stopped.events


def _ask_for_the_commit(
    state: LoopState,
    answer: FinalAnswer,
) -> tuple[LoopState, tuple[EventBody, ...]] | None:
    """Send a completed answer back once when nothing was committed.

    The same standard the reviewer is held to, applied to the implementer. A
    verdict claiming the tests passed must point at a green `run_tests` call;
    an answer reporting the task done claims a commit, because the Definition
    of Done says the change is one logical change in one commit, and that is
    checkable against this step's own log.

    Measured on the first L3 passes: the step ended with a complete checklist,
    `git_commit` was never called, the commit gate found nothing had landed,
    and the fix loop burned its revisions on work the hidden tests already
    accepted. Nobody checked the one claim the harness could check.

    An **incomplete** checklist is not touched. A model saying it is not done
    is being honest, and the handoff that follows is the designed ending.

    Asked once, and never when the step owes no commit or has no budget left
    to make one: telling a model to commit on a turn that cannot call a tool
    is the shape of refusal this project keeps removing.

    Returns:
        The state and events for the retry, or None when the answer stands.
    """
    if state.commit_asked or not state.config.requires_commit:
        return None
    if not answer.complete:
        return None
    missing = uncommitted_completion(state.calls)
    if not missing:
        return None
    if state.budget.must_finalize:
        return None
    asked = state.model_copy(
        update={
            "commit_asked": True,
            "pending_tool_choice": ToolChoice.AUTO,
            "pending_instruction": (
                f"This answer reports the task done, and {missing}. The gate has not "
                "run, so by this project's rules the work has not happened. Commit it "
                "with `git_commit`, which is the gate - or answer again with the "
                "checklist showing what is not done, and why, in NOTES.md."
            ),
        },
    )
    return asked, (
        ev.LoopIntervention(
            level=InterventionLevel.SYNTHETIC_RESULT,
            pathology="uncommitted_completion",
            detail=missing,
        ),
    )


def _ask_for_the_evidence(
    state: LoopState,
    verdict: Verdict,
) -> tuple[LoopState, tuple[EventBody, ...]] | None:
    """Send a verdict back once when its claims are not backed by this step.

    The harness does not touch the decision - a review is the model's to make,
    and §9 keeps the management surface out of that. What it does is refuse to
    record "the tests passed" from a step in which nothing ran them, and say
    so while there is still budget to go and run them.

    Asked once. A model that answers unbacked a second time has the verdict
    accepted, and the unearned claim is in the log for `evidence_ratio` to
    count: the alternative is a step that cannot finish, which is a wall with
    no door in front of a model that has made its judgement.

    Returns:
        The state and events for the retry, or None when the verdict stands.
    """
    if state.evidence_asked:
        return None
    unearned = unbacked_claims(
        state.calls,
        tests=verdict.tests_passed,
        build=verdict.build_passed,
    )
    if not unearned:
        return None
    if state.budget.must_finalize:
        # There is no budget left to run anything, and telling a model to run
        # the tests on a turn that cannot call a tool is the shape of refusal
        # this project keeps removing. The claim stands and the log records it.
        return None
    detail = "; ".join(unearned)
    asked = state.model_copy(
        update={
            "evidence_asked": True,
            # Tools back on: being told to run the tests on a turn that cannot
            # call one is the shape of refusal this project keeps removing.
            "pending_tool_choice": ToolChoice.AUTO,
            "pending_instruction": (
                f"That verdict claims evidence this step does not have: {detail}. "
                "Run it yourself and answer again. A reviewer that takes a claim on "
                "trust is the thing a reviewer is for."
            ),
        },
    )
    return asked, (
        ev.LoopIntervention(
            level=InterventionLevel.SYNTHETIC_RESULT,
            pathology="unbacked_claim",
            detail=detail,
        ),
    )


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
        duration_seconds=wall_seconds,
    )
    arguments_digest = _arguments_digest_of(pending)
    working = _spent(state, tool_calls=1, wall_seconds=wall_seconds)
    working = working.model_copy(
        update={
            "pending_call": None,
            # A failed call keeps its own answer, not a digest of it: the
            # repeat message hands it back, and a hash is nothing a model can
            # act on. A successful one keeps the digest, which is all the
            # message needs for "you already have this".
            "results_by_arguments": {
                **state.results_by_arguments,
                f"{pending.tool}:{arguments_digest}": (
                    str(digest) if result.ok else result.content
                ),
            },
            "failures_by_arguments": (
                state.failures_by_arguments
                if result.ok
                else state.failures_by_arguments | {f"{pending.tool}:{arguments_digest}"}
            ),
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
            failed=not result.ok,
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
            "failures_by_arguments": frozenset(),
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

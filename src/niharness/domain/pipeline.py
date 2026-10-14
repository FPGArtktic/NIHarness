# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pipeline.

Three entry points, one pipeline: brainstorm with a model, bring a SPEC.md, or
bring a finished plan and only execute it. All three pass the same gates; the
harness does not care who wrote the spec or the plan.

A stage boundary starts a fresh run seeded only with the artifact, so
conversation history never crosses a boundary. What matters must be in the
artifact.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.budgets import Budgets
from niharness.domain.roles import Role


class Stage(StrEnum):
    """A position in the pipeline, including the two terminal positions."""

    BRAINSTORM = "brainstorm"
    SPEC_GATE = "spec_gate"
    PLAN = "plan"
    PLAN_GATE = "plan_gate"
    APPROVAL = "approval"
    IMPLEMENT = "implement"
    COMMIT_GATE = "commit_gate"
    REVIEW = "review"
    FIX = "fix"
    FULL_GATE = "full_gate"
    COMPLETED = "completed"
    HANDOFF = "handoff"


TERMINAL_STAGES: frozenset[Stage] = frozenset({Stage.COMPLETED, Stage.HANDOFF})
"""Stages that end a run: finished, or returned to the human with full context."""

FRESH_RUN_STAGES: frozenset[Stage] = frozenset({Stage.PLAN, Stage.IMPLEMENT})
"""Stages that begin a new run, seeded only by SPEC.md or PLAN.md."""


class Outcome(StrEnum):
    """What a stage reported."""

    DONE = "done"
    """An LLM step produced its artifact."""

    GREEN = "green"
    RED = "red"
    ACK = "ACK"
    NAK = "NAK"
    APPROVED = "approved"
    REJECTED = "rejected"
    QUESTIONS = "questions"
    """The step is waiting for human answers; the stage does not change."""

    FAILED = "failed"


class StepDefinition(BaseModel):
    """What a stage runs: a role and a budget, or no model at all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    role: Role | None = Field(default=None, description="None for stages without a model.")
    budgets: Budgets
    acceptance: str = Field(description="The criterion the harness checks, not the model.")
    interactive: bool = False


PIPELINE: tuple[StepDefinition, ...] = (
    StepDefinition(
        stage=Stage.BRAINSTORM,
        role=Role.BRAINSTORMER,
        budgets=Budgets(max_tool_calls=60, max_tokens=200_000, max_wall_seconds=5_400),
        acceptance="SPEC.md written and schema-valid",
        interactive=True,
    ),
    StepDefinition(
        stage=Stage.SPEC_GATE,
        role=Role.REVIEWER,
        budgets=Budgets(max_tool_calls=0, max_tokens=20_000, max_wall_seconds=300),
        acceptance="schema valid and every acceptance criterion is testable; reviewer ACK",
    ),
    StepDefinition(
        stage=Stage.PLAN,
        role=Role.PLANNER,
        budgets=Budgets(max_tool_calls=25, max_tokens=120_000, max_wall_seconds=1_800),
        acceptance="PLAN.md validates against the Plan schema",
    ),
    StepDefinition(
        stage=Stage.PLAN_GATE,
        role=None,
        budgets=Budgets(max_tool_calls=0, max_tokens=0, max_wall_seconds=120),
        acceptance="Plan schema check passes; task ids unique and dependencies ordered",
    ),
    StepDefinition(
        stage=Stage.APPROVAL,
        role=None,
        budgets=Budgets(max_tool_calls=0, max_tokens=0, max_wall_seconds=0),
        acceptance=".nih/approved-plan.md exists and matches the plan digest",
    ),
    StepDefinition(
        stage=Stage.IMPLEMENT,
        role=Role.IMPLEMENTER,
        budgets=Budgets(max_tool_calls=40, max_tokens=150_000, max_wall_seconds=3_600),
        acceptance="FinalAnswer with a complete Definition of Done checklist",
    ),
    StepDefinition(
        stage=Stage.COMMIT_GATE,
        role=None,
        budgets=Budgets(max_tool_calls=0, max_tokens=0, max_wall_seconds=900),
        acceptance="quick gate green and the commit landed",
    ),
    StepDefinition(
        stage=Stage.REVIEW,
        role=Role.REVIEWER,
        budgets=Budgets(max_tool_calls=25, max_tokens=120_000, max_wall_seconds=1_800),
        acceptance="Verdict with ACK or NAK; the reviewer re-ran build and tests itself",
    ),
    StepDefinition(
        stage=Stage.FIX,
        role=Role.FIXER,
        budgets=Budgets(max_tool_calls=30, max_tokens=120_000, max_wall_seconds=2_400),
        acceptance="FinalAnswer addressing every blocker note",
    ),
    StepDefinition(
        stage=Stage.FULL_GATE,
        role=None,
        budgets=Budgets(max_tool_calls=0, max_tokens=0, max_wall_seconds=10_800),
        acceptance="full gate green: build, integration tests, sanitizers, docs link check",
    ),
)
"""The shipped pipeline. `harness.toml` may override budgets, never the order."""

DEFAULT_MAX_REVISIONS = 3
"""How many fix rounds a task gets before it returns to the human."""


class Transition(BaseModel):
    """Where the run goes next, and whether it starts a new run to get there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    revision: int = Field(ge=0)
    fresh_run: bool = False
    reason: str = ""

    @property
    def terminal(self) -> bool:
        """True when the run ends here."""
        return self.stage in TERMINAL_STAGES


def step_for(stage: Stage) -> StepDefinition:
    """Return the shipped step definition of `stage`.

    Raises:
        KeyError: for a terminal stage, which runs nothing.
    """
    for step in PIPELINE:
        if step.stage is stage:
            return step
    raise KeyError(stage)


def advance(  # noqa: C901 - the transition table is clearer as one function
    stage: Stage,
    outcome: Outcome,
    revision: int = 0,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
) -> Transition:
    """Return the transition out of `stage`.

    Args:
        stage: the stage that just reported.
        outcome: what it reported.
        revision: how many fix rounds this task has already had.
        max_revisions: the configured fix-loop bound.

    Returns:
        The next stage, the revision counter, and whether a fresh run starts.
        Exceeding the fix-loop bound hands the task back to the human.
    """
    if outcome is Outcome.QUESTIONS:
        return Transition(stage=stage, revision=revision, reason="waiting for human answers")
    if outcome is Outcome.FAILED:
        return Transition(
            stage=Stage.HANDOFF, revision=revision, reason=f"{stage.value} failed"
        )

    def to_fix(reason: str) -> Transition:
        if revision >= max_revisions:
            return Transition(
                stage=Stage.HANDOFF,
                revision=revision,
                reason=f"fix loop limit of {max_revisions} reached: {reason}",
            )
        return Transition(stage=Stage.FIX, revision=revision + 1, reason=reason)

    match (stage, outcome):
        case (Stage.BRAINSTORM, Outcome.DONE):
            return Transition(stage=Stage.SPEC_GATE, revision=revision, reason="spec drafted")
        case (Stage.SPEC_GATE, Outcome.GREEN) | (Stage.SPEC_GATE, Outcome.ACK):
            return Transition(
                stage=Stage.PLAN,
                revision=0,
                fresh_run=True,
                reason="spec accepted; fresh run seeded with SPEC.md",
            )
        case (Stage.SPEC_GATE, Outcome.RED) | (Stage.SPEC_GATE, Outcome.NAK):
            return Transition(stage=Stage.HANDOFF, revision=revision, reason="spec rejected")
        case (Stage.PLAN, Outcome.DONE):
            return Transition(stage=Stage.PLAN_GATE, revision=revision, reason="plan drafted")
        case (Stage.PLAN_GATE, Outcome.GREEN):
            return Transition(stage=Stage.APPROVAL, revision=revision, reason="plan valid")
        case (Stage.PLAN_GATE, Outcome.RED):
            return Transition(stage=Stage.HANDOFF, revision=revision, reason="plan invalid")
        case (Stage.APPROVAL, Outcome.APPROVED):
            return Transition(
                stage=Stage.IMPLEMENT,
                revision=0,
                fresh_run=True,
                reason="plan approved; fresh run seeded with PLAN.md",
            )
        case (Stage.APPROVAL, Outcome.REJECTED):
            return Transition(
                stage=Stage.HANDOFF, revision=revision, reason="plan not approved"
            )
        case (Stage.IMPLEMENT, Outcome.DONE) | (Stage.FIX, Outcome.DONE):
            return Transition(
                stage=Stage.COMMIT_GATE, revision=revision, reason="work reported done"
            )
        case (Stage.COMMIT_GATE, Outcome.GREEN):
            return Transition(stage=Stage.REVIEW, revision=revision, reason="commit landed")
        case (Stage.COMMIT_GATE, Outcome.RED):
            return to_fix("quick gate red")
        case (Stage.REVIEW, Outcome.ACK):
            return Transition(stage=Stage.FULL_GATE, revision=revision, reason="reviewer ACK")
        case (Stage.REVIEW, Outcome.NAK):
            return to_fix("reviewer NAK")
        case (Stage.FULL_GATE, Outcome.GREEN):
            return Transition(
                stage=Stage.COMPLETED, revision=revision, reason="full gate green"
            )
        case (Stage.FULL_GATE, Outcome.RED):
            return to_fix("full gate red")
        case _:
            return Transition(
                stage=Stage.HANDOFF,
                revision=revision,
                reason=f"unexpected outcome {outcome.value} in stage {stage.value}",
            )

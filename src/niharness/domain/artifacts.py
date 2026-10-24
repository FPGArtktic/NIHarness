# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline artifacts.

Every artifact that crosses a stage boundary is a Pydantic model, because a
stage boundary starts a fresh run seeded only with the artifact: whatever is
not in the artifact is lost. The JSON Schema of each model in this module is
exported to `schemas/` and is the contract with the TUI.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from niharness.domain.identity import Digest, digest_json

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that carries content after stripping."""

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
"""Lowercase identifier used for task and criterion ids."""

SCHEMA_VERSION: Literal[1] = 1
"""Bumped whenever an artifact model changes incompatibly."""


class Artifact(BaseModel):
    """Base class for artifacts: frozen, strict, versioned."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = SCHEMA_VERSION

    def digest(self) -> Digest:
        """Return a stable digest of this artifact's content."""
        return digest_json(self.model_dump(mode="json"))


class SpecOrigin(StrEnum):
    """Who wrote a spec. The pipeline treats all origins identically."""

    BRAINSTORM = "brainstorm"
    HUMAN = "human"
    EXTERNAL_AGENT = "external_agent"


class AcceptanceCriterion(Artifact):
    """One testable statement about the finished work."""

    id: Slug
    statement: NonEmpty
    verification: NonEmpty = Field(
        description="How the criterion is checked: a command, a test name, or an "
        "observable condition. Prose that cannot be executed or observed is a "
        "spec-gate failure.",
    )

    @model_validator(mode="after")
    def _verification_must_be_actionable(self) -> Self:
        """Reject verifications that only restate an intention."""
        vague = ("works", "is correct", "looks good", "should be fine", "tbd", "todo")
        lowered = self.verification.strip().lower()
        if lowered in vague or len(lowered) < 8:
            msg = f"criterion {self.id!r}: verification is not testable ({self.verification!r})"
            raise ValueError(msg)
        return self


class Spec(Artifact):
    """The accepted description of what to build. Entry A, B and C converge here."""

    title: NonEmpty
    origin: SpecOrigin
    goal: NonEmpty
    non_goals: tuple[NonEmpty, ...] = ()
    constraints: tuple[NonEmpty, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _criterion_ids_unique(self) -> Self:
        """Criterion ids are referenced from tasks, so they must be unique."""
        ids = [criterion.id for criterion in self.acceptance_criteria]
        duplicates = sorted({name for name in ids if ids.count(name) > 1})
        if duplicates:
            msg = f"duplicate acceptance criterion ids: {', '.join(duplicates)}"
            raise ValueError(msg)
        return self


class Task(Artifact):
    """One unit of implementation work: one logical change, one commit."""

    id: Slug
    title: NonEmpty
    intent: NonEmpty = Field(description="What changes and why, in the commit-message sense.")
    satisfies: tuple[Slug, ...] = Field(
        default=(),
        description="Ids of the acceptance criteria this task contributes to.",
    )
    depends_on: tuple[Slug, ...] = ()
    files: tuple[NonEmpty, ...] = Field(
        default=(),
        description="Paths the planner expects to touch; a hint, not a restriction.",
    )
    commit_type: NonEmpty = Field(
        default="feat",
        description="Conventional Commits type the gate will expect.",
    )


class Plan(Artifact):
    """An ordered, acyclic list of tasks derived from a spec."""

    spec_digest: Digest
    tasks: tuple[Task, ...] = Field(min_length=1)
    open_questions: tuple[NonEmpty, ...] = ()

    @model_validator(mode="after")
    def _tasks_form_a_dag_in_order(self) -> Self:
        """Ids are unique and dependencies point backwards, so list order is execution order."""
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                msg = f"duplicate task id: {task.id}"
                raise ValueError(msg)
            unmet = [dep for dep in task.depends_on if dep not in seen]
            if unmet:
                msg = (
                    f"task {task.id!r} depends on {', '.join(unmet)}, which do not "
                    "appear earlier in the plan"
                )
                raise ValueError(msg)
            seen.add(task.id)
        return self

    def task(self, task_id: str) -> Task:
        """Return the task with `task_id`.

        Raises:
            KeyError: if no task carries that id.
        """
        for candidate in self.tasks:
            if candidate.id == task_id:
                return candidate
        raise KeyError(task_id)


class PlanDraft(Artifact):
    """A plan as the planner writes it: the work, without the spec's digest.

    The digest is not something to ask a model for. It is sixty-four hex
    characters that the harness already knows, the planner can only transcribe
    it, and a plan rejected because the transcription slipped is a wall with no
    door in front of a model that did the thinking correctly. So the planner
    writes the tasks and the harness binds them to the spec they were planned
    from - see :meth:`for_spec`.

    Every rule about the tasks themselves still belongs to :class:`Plan`, and
    is applied at the moment of binding rather than relaxed.
    """

    tasks: tuple[Task, ...] = Field(min_length=1)
    open_questions: tuple[NonEmpty, ...] = Field(
        default=(),
        description="Decisions the spec leaves open that the planner had to assume.",
    )

    def for_spec(self, spec_digest: Digest) -> Plan:
        """Bind this draft to the spec it was planned from.

        Raises:
            ValueError: when the tasks do not form an ordered DAG - duplicate
                ids, or a dependency that points forwards. That check lives in
                :class:`Plan`, which is why binding is where it fires.
        """
        return Plan(
            spec_digest=spec_digest,
            tasks=self.tasks,
            open_questions=self.open_questions,
        )


class QuestionOption(Artifact):
    """One suggested answer rendered as a choice in the TUI picker."""

    id: Slug
    answer: NonEmpty
    consequence: str = ""


class Question(Artifact):
    """A clarifying question with suggested answers; free text is always allowed."""

    id: Slug
    question: NonEmpty
    options: tuple[QuestionOption, ...] = Field(default=(), max_length=4)
    allow_free_text: bool = True


class Questions(Artifact):
    """A bounded batch of clarifying questions emitted by a step."""

    questions: tuple[Question, ...] = Field(min_length=1, max_length=3)


class Answer(Artifact):
    """The human answer to one question, appended to the run as an event."""

    question_id: Slug
    option_id: Slug | None = None
    free_text: str = ""

    @model_validator(mode="after")
    def _answer_has_content(self) -> Self:
        """An answer is either a chosen option or free text."""
        if self.option_id is None and not self.free_text.strip():
            msg = f"answer to {self.question_id!r} is empty"
            raise ValueError(msg)
        return self


class Severity(StrEnum):
    """How much a reviewer note weighs."""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NOTE = "note"


class ReviewNote(Artifact):
    """A reviewer remark anchored to a file and, when possible, a line."""

    file: NonEmpty
    line: int | None = Field(default=None, ge=1)
    severity: Severity
    message: NonEmpty


class Decision(StrEnum):
    """The reviewer's verdict."""

    ACK = "ACK"
    NAK = "NAK"


class Verdict(Artifact):
    """A structured review result. The reviewer re-runs build and tests itself."""

    decision: Decision
    summary: NonEmpty
    notes: tuple[ReviewNote, ...] = ()
    build_passed: bool
    tests_passed: bool

    @model_validator(mode="after")
    def _ack_requires_green_and_no_blockers(self) -> Self:
        """An ACK on red evidence or over a blocker note is a contradiction."""
        if self.decision is Decision.ACK:
            if not (self.build_passed and self.tests_passed):
                msg = "ACK requires build_passed and tests_passed"
                raise ValueError(msg)
            blockers = [note for note in self.notes if note.severity is Severity.BLOCKER]
            if blockers:
                msg = f"ACK contradicts {len(blockers)} blocker note(s)"
                raise ValueError(msg)
        return self


class FailedAttempt(Artifact):
    """What was tried and why it did not work; carried across a step restart."""

    what: NonEmpty
    why_it_failed: NonEmpty


class ContextSummary(Artifact):
    """The utility model's replacement for turns compacted out of the window."""

    decisions: tuple[NonEmpty, ...] = ()
    open_questions: tuple[NonEmpty, ...] = ()
    files_touched: tuple[NonEmpty, ...] = ()
    failed_attempts: tuple[FailedAttempt, ...] = ()
    text: NonEmpty = Field(description="The prose form injected into the window.")


def ungrounded_paths(summary: ContextSummary, transcript: str) -> tuple[str, ...]:
    """Return the paths a summary claims that its source text never mentioned.

    The utility model is the smallest model in the harness, and the failure mode
    of a small summariser is not clumsy prose, it is invention: a file that was
    never touched, named confidently. A path in `files_touched` is the one claim
    that can be checked mechanically, so it is checked - the model's word is not
    evidence, here or anywhere else in this project.

    Args:
        summary: what the model produced.
        transcript: the text it was asked to summarise.

    Returns:
        The claimed paths that do not occur in the transcript, in order.
    """
    return tuple(path for path in summary.files_touched if path.strip("`'\"") not in transcript)


def grounded(summary: ContextSummary, transcript: str) -> ContextSummary:
    """Return `summary` with unsupported path claims removed."""
    invented = set(ungrounded_paths(summary, transcript))
    if not invented:
        return summary
    return summary.model_copy(
        update={
            "files_touched": tuple(
                path for path in summary.files_touched if path not in invented
            ),
        },
    )


class DodItem(Artifact):
    """One line of the Definition of Done checklist, filled by the model."""

    statement: NonEmpty
    done: bool
    evidence: str = ""


class FinalAnswer(Artifact):
    """A tool-using step's terminal output: a summary plus the filled checklist."""

    summary: NonEmpty
    dod: tuple[DodItem, ...] = Field(min_length=1)

    @property
    def complete(self) -> bool:
        """True when every checklist item is marked done."""
        return all(item.done for item in self.dod)


ARTIFACT_MODELS: tuple[type[Artifact], ...] = (
    Spec,
    Plan,
    PlanDraft,
    Questions,
    Answer,
    Verdict,
    ContextSummary,
    FinalAnswer,
)
"""Artifacts exported to `schemas/`; see `nih docs generate`."""

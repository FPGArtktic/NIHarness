# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoring a benchmark layer from what the harness recorded.

"The model is good" is not a measurement, and neither is a transcript somebody
read. L1 asks whether a model can call tools: were the calls valid, in a
sensible order, with the right arguments, and did the loop end on an answer
rather than on a budget or a detector.

All four are already in the event stream, so scoring is a **fold over a run's
events** rather than a second execution path. That matters for more than
elegance: the loop that runs a benchmark task is the same loop that runs real
work, with the same validation, the same detectors and the same budgets, so a
number here is about the model and not about a test harness that happens to
resemble one.

Everything in this module is pure. The expectations come from the suite's TOML,
the observations from `events.jsonl`, and nothing here runs a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from niharness.domain.events import (
    LoopIntervention,
    Payload,
    StepFinished,
    StopReason,
    ToolCalled,
    ToolRejected,
)

MAX_DETAIL = 240
"""How much of a refusal's own words the record carries."""

TERMINATING = frozenset({StopReason.FINAL_ANSWER})
"""The stop reasons that count as the model having finished.

A budget and a detector escalation are both the harness ending a step the
model did not end, which is the distinction `terminated_ratio` exists to draw.
"""


class L1Task(BaseModel):
    """One tool-calling task, with the sequence that answers it.

    The expectations are mechanical on purpose: a benchmark whose scoring needs
    a judgement cannot be re-run for a number six months later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expect_sequence: tuple[str, ...] = Field(
        default=(),
        description="Tool names in order. Empty for a task whose answer is a refusal.",
    )
    expect_arguments: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict,
        description="Per tool, the arguments that matter; others are not checked.",
    )
    expect_rejection: str = Field(
        default="",
        description="A `Violation` value the harness must produce, for a task that "
        "asks for something no role may do.",
    )
    strict_sequence: bool = Field(
        default=False,
        description="When false, extra calls around the expected ones are allowed: "
        "reading a file before patching it is not a mistake.",
    )
    max_tool_calls: int = Field(default=8, ge=1)


class TaskScore(BaseModel):
    """What one task's run showed. Every flag has a note when it is false."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    calls: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    interventions: int = Field(default=0, ge=0)
    violations: dict[str, int] = Field(
        default_factory=dict,
        description="How many calls each rule refused. `valid_calls_ratio` says how "
        "many were refused; this says what for, which is the actionable half.",
    )
    examples: dict[str, str] = Field(
        default_factory=dict,
        description="One refusal's own words per rule, truncated. A tally says which "
        "rule fired; this says what the model actually got wrong.",
    )
    sequence_ok: bool = False
    arguments_ok: bool = False
    rejection_ok: bool = True
    terminated: bool = False
    stop_reason: StopReason | None = None
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether this task was answered the way it was meant to be."""
        return self.sequence_ok and self.arguments_ok and self.rejection_ok and self.terminated

    @property
    def valid_calls(self) -> int:
        """Calls that passed validation."""
        return self.calls


class SuiteScore(BaseModel):
    """A layer's result, as the record format carries it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str = "L1"
    tasks: tuple[TaskScore, ...]

    @property
    def valid_calls_ratio(self) -> float:
        """Accepted calls over all calls proposed, rejections included."""
        accepted = sum(task.calls for task in self.tasks)
        refused = sum(task.rejected for task in self.tasks)
        return _ratio(accepted, accepted + refused)

    @property
    def correct_sequence_ratio(self) -> float:
        """Tasks whose tool sequence was the expected one."""
        return _ratio(sum(1 for task in self.tasks if task.sequence_ok), len(self.tasks))

    @property
    def correct_arguments_ratio(self) -> float:
        """Tasks where every pinned argument matched."""
        return _ratio(sum(1 for task in self.tasks if task.arguments_ok), len(self.tasks))

    @property
    def terminated_ratio(self) -> float:
        """Tasks the model ended itself, rather than a budget or a detector."""
        return _ratio(sum(1 for task in self.tasks if task.terminated), len(self.tasks))

    @property
    def passed_ratio(self) -> float:
        """Tasks that met every expectation."""
        return _ratio(sum(1 for task in self.tasks if task.passed), len(self.tasks))

    @property
    def loop_incidents_per_task(self) -> float:
        """Interventions per task.

        A first-class metric, not a footnote: a model that lands the right call
        after sixty redundant ones is not the same as one that lands it in
        eight, and no ratio above tells them apart.
        """
        return _ratio(sum(task.interventions for task in self.tasks), len(self.tasks), scale=1)

    def violations(self) -> dict[str, int]:
        """Every rule that refused a call, with how often, across the suite.

        `valid_calls_ratio` says how much a model got wrong; this says what,
        and only the second one tells anybody what to change.
        """
        tally: dict[str, int] = {}
        for task in self.tasks:
            for rule, count in task.violations.items():
                tally[rule] = tally.get(rule, 0) + count
        return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))

    def examples(self) -> dict[str, str]:
        """One refusal's words per rule, so the tally explains itself."""
        found: dict[str, str] = {}
        for task in self.tasks:
            for rule, detail in task.examples.items():
                found.setdefault(rule, detail)
        return dict(sorted(found.items()))

    def metrics(self) -> dict[str, float]:
        """Return the metrics the suite declares, by the names it declares them."""
        return {
            "valid_calls_ratio": self.valid_calls_ratio,
            "correct_sequence_ratio": self.correct_sequence_ratio,
            "correct_arguments_ratio": self.correct_arguments_ratio,
            "terminated_ratio": self.terminated_ratio,
            "loop_incidents_per_task": self.loop_incidents_per_task,
            "passed_ratio": self.passed_ratio,
        }


def score_task(task: L1Task, events: Sequence[Payload]) -> TaskScore:
    """Score one task from the events its run produced.

    Args:
        task: the expectations, from the suite.
        events: the payloads of that task's step, in order.

    Returns:
        The score, with a note for every expectation that was not met.
    """
    called = [event for event in events if isinstance(event, ToolCalled)]
    refused = [event for event in events if isinstance(event, ToolRejected)]
    interventions = [event for event in events if isinstance(event, LoopIntervention)]
    finished = next(
        (event for event in reversed(events) if isinstance(event, StepFinished)), None
    )
    seen = tuple(event.name for event in called)

    notes: list[str] = []
    sequence_ok = _sequence_ok(task, seen)
    if not sequence_ok:
        wanted = " -> ".join(task.expect_sequence) or "(no calls)"
        notes.append(f"sequence: wanted {wanted}, saw {' -> '.join(seen) or '(none)'}")

    arguments_ok, argument_note = _arguments_ok(task, called)
    if argument_note:
        notes.append(argument_note)

    rejection_ok = True
    if task.expect_rejection:
        rejection_ok = any(event.violation == task.expect_rejection for event in refused)
        if not rejection_ok:
            saw = ", ".join(sorted({event.violation for event in refused})) or "nothing refused"
            notes.append(f"rejection: wanted {task.expect_rejection}, saw {saw}")

    terminated = finished is not None and finished.stop_reason in TERMINATING
    if not terminated:
        reason = finished.stop_reason.value if finished else "no step.finished"
        notes.append(f"did not terminate on an answer: {reason}")

    return TaskScore(
        task_id=task.id,
        calls=len(called),
        rejected=len(refused),
        interventions=len(interventions),
        violations=_counted(event.violation for event in refused),
        examples=_examples(refused),
        sequence_ok=sequence_ok,
        arguments_ok=arguments_ok,
        rejection_ok=rejection_ok,
        terminated=terminated,
        stop_reason=finished.stop_reason if finished else None,
        notes=tuple(notes),
    )


def score_suite(scores: Iterable[TaskScore], *, layer: str = "L1") -> SuiteScore:
    """Collect task scores into a layer's result."""
    return SuiteScore(layer=layer, tasks=tuple(scores))


def is_subsequence(wanted: Sequence[str], seen: Sequence[str]) -> bool:
    """Return whether `wanted` appears in `seen` in order, not necessarily adjacently.

    What "the right sequence" means when extra calls are allowed: reading a
    file before patching it, or grepping twice, is not a mistake.
    """
    remaining = iter(seen)
    return all(any(item == candidate for candidate in remaining) for item in wanted)


def _sequence_ok(task: L1Task, seen: Sequence[str]) -> bool:
    """Whether the observed calls satisfy the task's expected sequence."""
    if task.strict_sequence:
        return tuple(seen) == task.expect_sequence
    if task.expect_rejection:
        # What a refusal task measures is that the rule fired, which
        # `rejection_ok` checks. The refused call never executed - that is what
        # a refusal is - so the model cannot have done the forbidden thing by
        # any route, and a harmless call afterwards is not a failure. The first
        # L1 run failed `refuse-escape` for a `list_tree`, which was the
        # expectation being wrong rather than the model.
        return True
    if not task.expect_sequence:
        return not seen
    return is_subsequence(task.expect_sequence, seen)


def _arguments_ok(task: L1Task, called: Sequence[ToolCalled]) -> tuple[bool, str]:
    """Check the pinned arguments of the first call to each named tool."""
    for name, expected in sorted(task.expect_arguments.items()):
        match = next((event for event in called if event.name == name), None)
        if match is None:
            return False, f"arguments: {name} was never called"
        for key, value in sorted(expected.items()):
            if match.arguments.get(key) != value:
                got = match.arguments.get(key, "(absent)")
                return False, f"arguments: {name}.{key} wanted {value!r}, got {got!r}"
    return True, ""


def _ratio(part: int, whole: int, *, scale: float = 1.0) -> float:
    """Return `part / whole`, or zero when there is nothing to divide."""
    return round(part / whole * scale, 4) if whole else 0.0


def _examples(refused: Sequence[ToolRejected]) -> dict[str, str]:
    """Return the first refusal's detail for each rule, truncated."""
    found: dict[str, str] = {}
    for event in refused:
        trimmed = " ".join(event.detail.split())
        found.setdefault(
            event.violation,
            trimmed if len(trimmed) <= MAX_DETAIL else f"{trimmed[:MAX_DETAIL]}...",
        )
    return found


def _counted(values: Iterable[str]) -> dict[str, int]:
    """Return how many times each value occurred, most frequent first."""
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))

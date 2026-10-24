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

from collections.abc import Iterable, Mapping, Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from niharness.domain.artifacts import Decision, Verdict
from niharness.domain.events import (
    LoopIntervention,
    Payload,
    StepFinished,
    StopReason,
    ToolCalled,
    ToolRejected,
    ToolReturned,
)
from niharness.domain.tools import READ_ONLY_TOOLS, ToolName

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
    expect_success: tuple[str, ...] = Field(
        default=(),
        description="Tools whose last call must have *worked*. Being called is not "
        "the same as having an effect: a model whose every edit was refused and "
        "who then ran the tests would otherwise score the sequence.",
    )
    expect_content: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Per path, text that must be in the file when the step ends. The "
        "one expectation about the *end state* rather than the route: a change that "
        "two tools could make equally well should not be scored on which was used.",
    )
    forbid_content: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Per path, text that must be **gone** when the step ends. The "
        "mirror of `expect_content`, and the only way to score a removal: asserting "
        "what is present cannot say that something is not.",
    )
    forbid_paths: tuple[str, ...] = Field(
        default=(),
        description="Path prefixes no call may name. For a task whose point is "
        "restraint - the policy sends a wrong test to NOTES.md rather than "
        "editing it quietly.",
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
    pathologies: dict[str, int] = Field(
        default_factory=dict,
        description="How many interventions each pathology caused. `interventions` "
        "says how often the ladder was climbed; this says what for, and a change to "
        "the ladder cannot be measured without it.",
    )
    tools: dict[str, int] = Field(
        default_factory=dict,
        description="How often each tool was called. `calls` says how much work the "
        "step did; this says what of, which is the difference between a step that "
        "ran the tests and one that read four files.",
    )
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
    complete: bool = True
    effective: bool = True
    restrained: bool = True
    rejection_ok: bool = True
    terminated: bool = False
    stop_reason: StopReason | None = None
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether this task was answered the way it was meant to be."""
        return (
            self.sequence_ok
            and self.arguments_ok
            and self.complete
            and self.effective
            and self.restrained
            and self.rejection_ok
            and self.terminated
        )

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
    def complete_ratio(self) -> float:
        """Tasks whose end state is what was asked for, whatever route reached it."""
        return _ratio(sum(1 for task in self.tasks if task.complete), len(self.tasks))

    @property
    def effective_ratio(self) -> float:
        """Tasks where the tools that had to work did work."""
        return _ratio(sum(1 for task in self.tasks if task.effective), len(self.tasks))

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

    def pathologies(self) -> dict[str, int]:
        """Every pathology a detector fired on, with how often, across the suite.

        `loop_incidents_per_task` says how much interference there was; this
        says which detector caused it, which is what a change to the ladder is
        measured against.
        """
        return _tallied(task.pathologies for task in self.tasks)

    def tools(self) -> dict[str, int]:
        """Every tool the suite called, with how often, across every task."""
        return _tallied(task.tools for task in self.tasks)

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

    def outcomes(self) -> tuple[tuple[str, bool], ...]:
        """Each task's id and whether it passed, for the across-passes report."""
        return tuple((task.task_id, task.passed) for task in self.tasks)

    def metrics(self) -> dict[str, float]:
        """Return the metrics the suite declares, by the names it declares them."""
        return {
            "valid_calls_ratio": self.valid_calls_ratio,
            "correct_sequence_ratio": self.correct_sequence_ratio,
            "correct_arguments_ratio": self.correct_arguments_ratio,
            "effective_ratio": self.effective_ratio,
            "complete_ratio": self.complete_ratio,
            "terminated_ratio": self.terminated_ratio,
            "loop_incidents_per_task": self.loop_incidents_per_task,
            "passed_ratio": self.passed_ratio,
        }


def score_task(
    task: L1Task,
    events: Sequence[Payload],
    files: Mapping[str, str] | None = None,
) -> TaskScore:
    """Score one task from the events its run produced.

    Args:
        task: the expectations, from the suite.
        events: the payloads of that task's step, in order.
        files: the content of each path in `expect_content` or
            `forbid_content`, read after the
            step. Absent means the task declares no end state, and a task that
            does while this is empty fails - which is the right way round: a
            caller that forgot to read the files should not score a pass.

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

    complete, completion_note = _complete(task, files or {})
    if completion_note:
        notes.append(completion_note)

    effective, effect_note = _effective(task, called, events)
    if effect_note:
        notes.append(effect_note)

    restrained, restraint_note = _restrained(task, called)
    if restraint_note:
        notes.append(restraint_note)

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
        pathologies=_counted(event.pathology for event in interventions),
        tools=_counted(event.name for event in called),
        violations=_counted(event.violation for event in refused),
        examples=_examples(refused),
        sequence_ok=sequence_ok,
        arguments_ok=arguments_ok,
        complete=complete,
        effective=effective,
        restrained=restrained,
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
        # An empty sequence demands silence only when the task expects nothing
        # else. A task that declares an end state is saying it does not care
        # which route reached it, and that includes not caring how many calls
        # it took.
        return bool(task.expect_content) or not seen
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


def _complete(task: L1Task, files: Mapping[str, str]) -> tuple[bool, str]:
    """Check the end state the task declares, without caring how it was reached.

    The complement to the event fold rather than a replacement for it: the
    events say how hard the model worked, and this says whether the work is
    there. A task that two tools could satisfy equally well should not be
    scored on which one was used.
    """
    for path, wanted in sorted(task.expect_content.items()):
        content = files.get(path)
        if content is None:
            return False, f"end state: {path} was not read back, or is not there"
        for fragment in wanted:
            if fragment not in content:
                return False, f"end state: {path} does not contain {fragment!r}"
    for path, unwanted in sorted(task.forbid_content.items()):
        content = files.get(path)
        if content is None:
            return False, f"end state: {path} was not read back, or is not there"
        for fragment in unwanted:
            if fragment in content:
                return False, f"end state: {path} still contains {fragment!r}"
    return True, ""


def _effective(
    task: L1Task,
    called: Sequence[ToolCalled],
    events: Sequence[Payload],
) -> tuple[bool, str]:
    """Check that each tool in `expect_success` ended up working.

    The *last* call to the tool, because a model that is refused and then gets
    it right has done the task - recovering from a refusal is a skill, not a
    failure. What is not acceptable is finishing with the edit still refused.
    """
    if not task.expect_success:
        return True, ""
    results = {event.call_id: event.ok for event in events if isinstance(event, ToolReturned)}
    for name in task.expect_success:
        attempts = [event for event in called if event.name == name]
        if not attempts:
            return False, f"effect: {name} was never called, so nothing it does happened"
        if not results.get(attempts[-1].call_id, False):
            return False, f"effect: the last {name} call did not succeed"
    return True, ""


def _restrained(task: L1Task, called: Sequence[ToolCalled]) -> tuple[bool, str]:
    """Check that nothing *wrote* to a path the task puts out of bounds.

    Reading is not touching. A model told that a test encodes the right
    behaviour should read that test; what the policy forbids is editing it
    quietly, so only a writing tool can break this.
    """
    for event in called:
        named = event.arguments.get("path")
        if not isinstance(named, str) or _reads_only(event.name):
            continue
        for forbidden in task.forbid_paths:
            if named.startswith(forbidden):
                return (
                    False,
                    f"restraint: {event.name} wrote to {named}, which is out of bounds",
                )
    return True, ""


def _reads_only(name: str) -> bool:
    """Whether a tool cannot change the worktree.

    A name the harness does not know is treated as writing, which is the
    conservative way round: a declared server's tool could do anything.
    """
    try:
        return ToolName(name) in READ_ONLY_TOOLS
    except ValueError:
        return False


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


def _tallied(tallies: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """Sum several tallies into one, most frequent first."""
    total: dict[str, int] = {}
    for tally in tallies:
        for key, count in tally.items():
            total[key] = total.get(key, 0) + count
    return dict(sorted(total.items(), key=lambda item: (-item[1], item[0])))


def _counted(values: Iterable[str]) -> dict[str, int]:
    """Return how many times each value occurred, most frequent first."""
    tally: dict[str, int] = {}
    for value in values:
        tally[value] = tally.get(value, 0) + 1
    return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0])))


# ------------------------------------------------------------------------- L2

NAK_IS_THE_POSITIVE_CLASS = """Precision and recall take NAK as the positive class.

The reviewer's job is finding the defect, so a NAK is a detection: precision
is how many of its NAKs were earned, recall is how many of the real defects it
caught. Stated here because the choice is not symmetric and the numbers mean
the opposite if it is read the other way round.
"""


class L2Case(BaseModel):
    """One change under review, with the verdict a human gave it.

    A case is the fixture plus a directory of replacement files, committed as
    one change. Not a stored `.patch`: a patch can stop applying when the
    fixture moves, and a golden set whose cases silently fail to apply is
    worse than no golden set. The diff the reviewer reads is produced by git
    from the two trees, which is also exactly how a real review gets one.

    Every case is **green**. L2 measures review, and the gate is what review
    happens after - so a defect that fails the tests never reaches a reviewer
    in the real pipeline and has no business in this suite. Every NAK here is
    a defect the tests do not catch, which is the only kind worth a reviewer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1, description="The task the change claims to do.")
    intent: str = Field(min_length=1, description="Why, in the commit-message sense.")
    commit_type: str = Field(default="feat", description="Conventional Commits type.")
    verdict: Decision = Field(description="The human verdict this case is scored against.")
    defect_files: tuple[str, ...] = Field(
        default=(),
        description="Where the defect is. A note anchored to any one of them counts as "
        "located: a stale document and the code that outdated it are both right answers.",
    )
    mentions_any: tuple[str, ...] = Field(
        default=(),
        description="Tokens, at least one of which must appear in the verdict's own "
        "words. Any rather than all, because a reviewer's prose is free text and "
        "demanding a phrasing would measure vocabulary rather than review.",
    )
    why: str = Field(default="", description="The human's reason, for the report.")
    max_tool_calls: int = Field(default=12, ge=1)

    @model_validator(mode="after")
    def _the_right_answer_is_spelled_out(self) -> Self:
        """A NAK case says where the defect is and what naming it looks like.

        Without this a NAK case could be added with no answer key, and the
        suite would score "said NAK" - which a model that NAKs everything
        passes with full marks.
        """
        if self.verdict is Decision.NAK and not (self.defect_files and self.mentions_any):
            msg = (
                f"case {self.id!r}: a NAK case needs defect_files and mentions_any, "
                "or it scores a reviewer that refuses everything"
            )
            raise ValueError(msg)
        if self.verdict is Decision.ACK and (self.defect_files or self.mentions_any):
            msg = f"case {self.id!r}: an ACK case has no defect to locate"
            raise ValueError(msg)
        return self


class CaseScore(BaseModel):
    """What one review showed, against the human verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    expected: Decision
    decided: Decision | None = Field(
        default=None,
        description="What the reviewer said. None means the step produced no verdict "
        "at all, which is neither an ACK nor a NAK and must not be scored as one.",
    )
    located: bool = False
    reasoned: bool = False
    evidence_ok: bool = Field(
        default=False,
        description="Whether the verdict's claim that the tests passed was earned by "
        "running them in this step. The reviewer's whole job is not trusting a claim; "
        "this is the same test applied to the reviewer.",
    )
    terminated: bool = False
    calls: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    interventions: int = Field(default=0, ge=0)
    highest_intervention: int = Field(default=0, ge=0)
    pathologies: dict[str, int] = Field(
        default_factory=dict,
        description="How many interventions each pathology caused. Without it a "
        "clean `evidence_ratio` is consistent with the evidence check firing and "
        "with its never having fired at all.",
    )
    tools: dict[str, int] = Field(
        default_factory=dict,
        description="How often each tool was called. A reviewer that refused a "
        "correct change because 'the tests cannot be run' either called `run_tests` "
        "and saw something, or never called it - and a count of calls cannot tell "
        "those apart.",
    )
    diff_lines: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = ()

    @property
    def decision_ok(self) -> bool:
        """Whether the reviewer reached the human's verdict."""
        return self.decided is not None and self.decided is self.expected

    @property
    def passed(self) -> bool:
        """Whether this review was the review that was asked for.

        A NAK has to be for the right reason and in the right place; an ACK
        has only one way to be right. Both have to be backed by a test run,
        because §4 says the reviewer re-runs the tests itself.
        """
        if not (self.decision_ok and self.evidence_ok and self.terminated):
            return False
        if self.expected is Decision.NAK:
            return self.located and self.reasoned
        return True


class L2Score(BaseModel):
    """The review layer's result, as the record format carries it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str = "L2"
    cases: tuple[CaseScore, ...]

    @property
    def true_positives(self) -> int:
        """Real defects the reviewer refused."""
        return sum(
            1
            for case in self.cases
            if case.expected is Decision.NAK and case.decided is Decision.NAK
        )

    @property
    def false_positives(self) -> int:
        """Correct changes the reviewer refused."""
        return sum(
            1
            for case in self.cases
            if case.expected is Decision.ACK and case.decided is Decision.NAK
        )

    @property
    def false_negatives(self) -> int:
        """Real defects the reviewer waved through."""
        return sum(
            1
            for case in self.cases
            if case.expected is Decision.NAK and case.decided is Decision.ACK
        )

    @property
    def undecided(self) -> int:
        """Cases that produced no verdict, counted as neither right nor wrong.

        A step that ran out of budget did not review anything, and folding
        that into recall would make a model look cautious when it was absent.
        """
        return sum(1 for case in self.cases if case.decided is None)

    @property
    def reviewer_precision(self) -> float:
        """Refusals that were earned."""
        return _ratio(self.true_positives, self.true_positives + self.false_positives)

    @property
    def reviewer_recall(self) -> float:
        """Real defects caught."""
        return _ratio(self.true_positives, self.true_positives + self.false_negatives)

    @property
    def decision_ratio(self) -> float:
        """Cases where the reviewer reached the human's verdict at all."""
        return _ratio(sum(1 for case in self.cases if case.decision_ok), len(self.cases))

    @property
    def located_ratio(self) -> float:
        """Of the defects it caught, how many it anchored to the right file."""
        caught = [
            case
            for case in self.cases
            if case.expected is Decision.NAK and case.decided is Decision.NAK
        ]
        return _ratio(sum(1 for case in caught if case.located), len(caught))

    @property
    def reasoned_ratio(self) -> float:
        """Of the defects it caught, how many it caught for the right reason."""
        caught = [
            case
            for case in self.cases
            if case.expected is Decision.NAK and case.decided is Decision.NAK
        ]
        return _ratio(sum(1 for case in caught if case.reasoned), len(caught))

    @property
    def evidence_ratio(self) -> float:
        """Verdicts whose claim about the tests was earned by running them."""
        decided = [case for case in self.cases if case.decided is not None]
        return _ratio(sum(1 for case in decided if case.evidence_ok), len(decided))

    @property
    def terminated_ratio(self) -> float:
        """Cases the model ended itself, rather than a budget or a detector."""
        return _ratio(sum(1 for case in self.cases if case.terminated), len(self.cases))

    @property
    def passed_ratio(self) -> float:
        """Cases that met every expectation."""
        return _ratio(sum(1 for case in self.cases if case.passed), len(self.cases))

    @property
    def loop_incidents_per_case(self) -> float:
        """Interventions per case."""
        return _ratio(sum(case.interventions for case in self.cases), len(self.cases), scale=1)

    @property
    def highest_intervention_level(self) -> float:
        """The worst the ladder had to climb anywhere in the suite."""
        return float(max((case.highest_intervention for case in self.cases), default=0))

    @property
    def median_diff_lines(self) -> float:
        """How big the suite's changes are. A property of the set, not the model."""
        sizes = sorted(case.diff_lines for case in self.cases)
        if not sizes:
            return 0.0
        middle = len(sizes) // 2
        if len(sizes) % 2 == 1:
            return float(sizes[middle])
        return (sizes[middle - 1] + sizes[middle]) / 2

    def pathologies(self) -> dict[str, int]:
        """Every pathology a detector fired on, with how often, across the suite."""
        return _tallied(case.pathologies for case in self.cases)

    def tools(self) -> dict[str, int]:
        """Every tool the suite called, with how often, across every case."""
        return _tallied(case.tools for case in self.cases)

    def outcomes(self) -> tuple[tuple[str, bool], ...]:
        """Each case's id and whether it passed, for the across-passes report."""
        return tuple((case.case_id, case.passed) for case in self.cases)

    def metrics(self) -> dict[str, float]:
        """Return the metrics the suite declares, by the names it declares them."""
        return {
            "reviewer_precision": self.reviewer_precision,
            "reviewer_recall": self.reviewer_recall,
            "decision_ratio": self.decision_ratio,
            "located_ratio": self.located_ratio,
            "reasoned_ratio": self.reasoned_ratio,
            "evidence_ratio": self.evidence_ratio,
            "terminated_ratio": self.terminated_ratio,
            "passed_ratio": self.passed_ratio,
            "loop_incidents_per_case": self.loop_incidents_per_case,
            "highest_intervention_level": self.highest_intervention_level,
            "median_diff_lines": self.median_diff_lines,
        }


def score_case(
    case: L2Case,
    verdict: Verdict | None,
    events: Sequence[Payload],
    *,
    diff_lines: int = 0,
) -> CaseScore:
    """Score one review against the human verdict.

    Args:
        case: the case and its answer key.
        verdict: what the reviewer produced, or None when the step produced
            nothing. Passed in rather than read from the log because an
            artifact is a step's end state and the log records what a step
            *did*; L1 reads the files it changed for the same reason.
        events: the payloads of that step, in order.
        diff_lines: how big the change under review was.

    Returns:
        The score, with a note for every expectation that was not met.
    """
    called = [body for body in events if isinstance(body, ToolCalled)]
    refused = [body for body in events if isinstance(body, ToolRejected)]
    interventions = [body for body in events if isinstance(body, LoopIntervention)]
    finished = [body for body in events if isinstance(body, StepFinished)]
    stop = finished[-1].stop_reason if finished else None

    notes: list[str] = []
    if verdict is None:
        notes.append(f"no verdict: the step ended on {stop.value if stop else 'nothing'}")
    words = _verdict_words(verdict)
    located = bool(verdict) and any(
        _same_path(note.file, wanted)
        for note in (verdict.notes if verdict else ())
        for wanted in case.defect_files
    )
    reasoned = any(token.lower() in words for token in case.mentions_any)
    if verdict is not None and case.verdict is Decision.NAK:
        if verdict.decision is Decision.ACK:
            notes.append(f"waved through: {case.why or 'the defect was not found'}")
        elif not located:
            anchored = ", ".join(note.file for note in verdict.notes) or "nothing"
            notes.append(f"anchored to {anchored}, not to {', '.join(case.defect_files)}")
        elif not reasoned:
            notes.append(f"none of {', '.join(case.mentions_any)} appears in the notes")
    if (
        verdict is not None
        and case.verdict is Decision.ACK
        and verdict.decision is Decision.NAK
    ):
        notes.append(f"refused a correct change: {verdict.summary}")

    evidence_ok, evidence_note = _evidence(verdict, called, events)
    if evidence_note:
        notes.append(evidence_note)

    return CaseScore(
        case_id=case.id,
        expected=case.verdict,
        decided=verdict.decision if verdict else None,
        located=located,
        reasoned=reasoned,
        evidence_ok=evidence_ok,
        terminated=stop in TERMINATING,
        calls=len(called),
        rejected=len(refused),
        interventions=len(interventions),
        highest_intervention=max((int(body.level) for body in interventions), default=0),
        pathologies=_counted(body.pathology for body in interventions),
        tools=_counted(body.name for body in called),
        diff_lines=diff_lines,
        notes=tuple(notes),
    )


def score_reviews(scores: Iterable[CaseScore]) -> L2Score:
    """Assemble the layer's score from its cases."""
    return L2Score(cases=tuple(scores))


def _verdict_words(verdict: Verdict | None) -> str:
    """Return everything the reviewer said, lowercased, for a token search."""
    if verdict is None:
        return ""
    parts = [verdict.summary, *(f"{note.file} {note.message}" for note in verdict.notes)]
    return " ".join(parts).lower()


def _same_path(seen: str, wanted: str) -> bool:
    """Whether a note's file is the one the case points at.

    A reviewer may write the path as the diff shows it, with or without a
    leading `a/` or `./`, so the comparison is on the tail rather than exact.
    """
    left = seen.replace("\\", "/").lstrip("./").removeprefix("a/").removeprefix("b/")
    return left == wanted or left.endswith(f"/{wanted}") or wanted.endswith(f"/{left}")


def _evidence(
    verdict: Verdict | None,
    called: Sequence[ToolCalled],
    events: Sequence[Payload],
) -> tuple[bool, str]:
    """Whether the verdict's claim about the tests was earned in this step.

    The reviewer exists because an implementer's claim is not evidence. The
    same standard is applied here: a verdict that says the tests passed has to
    point at a `run_tests` call in its own step that returned green.
    """
    if verdict is None:
        return False, ""
    if not verdict.tests_passed:
        return True, ""
    ids = {body.call_id for body in called if body.name == ToolName.RUN_TESTS.value}
    green = any(
        isinstance(body, ToolReturned) and body.call_id in ids and body.ok for body in events
    )
    if green:
        return True, ""
    ran = "never ran the tests" if not ids else "ran the tests and they did not pass"
    return False, f"claimed the tests passed but {ran}"


# ------------------------------------------------------------------------- L0

MIN_GENERATED_FOR_A_RATE = 2
"""Two tokens at least: the rate is derived from the gap between the first and the rest."""


class L0Probe(BaseModel):
    """One throughput measurement: how long a prompt, and how much to generate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_tokens: int = Field(ge=64, description="Prompt length to measure at.")
    generate_tokens: int = Field(
        default=64,
        ge=MIN_GENERATED_FOR_A_RATE,
        description="Tokens to generate in the long request.",
    )

    @property
    def id(self) -> str:
        """A readable name for this probe, used in records and progress lines."""
        return f"{_thousands(self.context_tokens)}-ctx"


class Throughput(BaseModel):
    """What one probe measured, and what it could not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    context_tokens: int = Field(ge=0, description="What was asked for.")
    prompt_tokens: int = Field(ge=0, description="What the tokenizer actually counted.")
    generated: int = Field(ge=0)
    cold_seconds: float = Field(ge=0.0, description="One token on a prompt never seen.")
    warm_seconds: float = Field(ge=0.0, description="One token on the same prompt again.")
    long_seconds: float = Field(ge=0.0, description="The full generation on it.")
    prompt_tps: float = Field(ge=0.0)
    generation_tps: float = Field(ge=0.0)
    notes: tuple[str, ...] = ()

    @property
    def measured(self) -> bool:
        """Whether both rates came out of the arithmetic."""
        return self.prompt_tps > 0.0 and self.generation_tps > 0.0


def throughput(
    probe: L0Probe,
    *,
    prompt_tokens: int,
    generated: int,
    cold_seconds: float,
    warm_seconds: float,
    long_seconds: float,
) -> Throughput:
    """Derive prompt-processing and generation rates from three timed requests.

    Three requests, same prompt, in this order: one generating a single token
    on a prompt the server has never seen, one generating a single token on
    the same prompt, and one generating `generated` tokens on it again.

    The server's prefix cache is the instrument rather than the enemy. The
    second request repeats the first exactly, so it pays the per-request
    overhead and one token of generation and nothing else. Subtracting it from
    the first leaves the prompt; subtracting it from the third leaves the
    generation:

    - `generation_tps = (generated - 1) / (long_seconds - warm_seconds)`
    - `prompt_tps = prompt_tokens / (cold_seconds - warm_seconds)`

    No backend timings are read. The port is OpenAI-compatible HTTP and
    nothing more, and a measurement that depended on `llama.cpp` reporting its
    own numbers would be a backend-specific code path with extra steps.

    The design is also self-checking, which the two-request version was not.
    If the first request was *not* cold - the prompt was already cached from
    an earlier run - then `cold_seconds` and `warm_seconds` are the same and
    the prompt rate is reported as unmeasured instead of as a number like
    68,000 tok/s. That number is not hypothetical: it is what the two-request
    version produced once the markers it used started repeating between runs.

    Args:
        probe: what was asked for.
        prompt_tokens: what the tokenizer counted for the prompt that was sent.
        generated: completion tokens the long request produced.
        cold_seconds: wall time of the first single-token request.
        warm_seconds: wall time of the repeated single-token request.
        long_seconds: wall time of the long request.

    Returns:
        The sample, with a note for each rate the arithmetic could not produce.
    """
    notes: list[str] = []
    produced = generated - 1
    generation_gap = long_seconds - warm_seconds
    generation_tps = 0.0
    if produced >= 1 and generation_gap > 0:
        generation_tps = produced / generation_gap
    else:
        notes.append(
            f"no generation rate: {produced} token(s) over {generation_gap:.3f}s. "
            "The long request has to be slower than the single-token one and "
            "produce more than one token, or there is nothing to divide."
        )

    prompt_gap = cold_seconds - warm_seconds
    prompt_tps = 0.0
    # The repeated request is this method's own resolution: it is what a
    # request costs when it does almost nothing. A prompt whose processing
    # took less than that cannot be timed this way - either the cache already
    # had it, or the prompt is too short to measure through HTTP. Both are
    # reported as unmeasured rather than as the enormous number that dividing
    # by a millisecond produces. Measured once: 68,000 tok/s on a laptop GPU.
    if prompt_gap > warm_seconds and prompt_tokens > 0:
        prompt_tps = prompt_tokens / prompt_gap
    else:
        notes.append(
            f"no prompt rate: processing the prompt took {prompt_gap:.3f}s, against "
            f"{warm_seconds:.3f}s for a request that did nothing. Either the prompt "
            "was already cached and this run never processed it, or it is too short "
            "to time through HTTP. Either way the number would be the method's, not "
            "the host's."
        )
    return Throughput(
        probe_id=probe.id,
        context_tokens=probe.context_tokens,
        prompt_tokens=prompt_tokens,
        generated=generated,
        cold_seconds=round(cold_seconds, 3),
        warm_seconds=round(warm_seconds, 3),
        long_seconds=round(long_seconds, 3),
        prompt_tps=round(prompt_tps, 1),
        generation_tps=round(generation_tps, 2),
        notes=tuple(notes),
    )


class L0Score(BaseModel):
    """The inference layer's result: rates per context size, nothing graded.

    There is no pass or fail here. L0 says how fast the host is, and every
    step budget in the harness is fitted to those numbers - which is why it
    belongs in the same record format as the layers that do grade something.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str = "L0"
    samples: tuple[Throughput, ...]

    def outcomes(self) -> tuple[tuple[str, bool], ...]:
        """Nothing passes or fails, so the across-passes table has no rows."""
        return ()

    def metrics(self) -> dict[str, float]:
        """Both rates at each context size, named by it."""
        found: dict[str, float] = {}
        for sample in self.samples:
            size = _thousands(sample.context_tokens)
            found[f"prompt_tps_{size}"] = sample.prompt_tps
            found[f"generation_tps_{size}"] = sample.generation_tps
        return found


def score_throughput(samples: Iterable[Throughput]) -> L0Score:
    """Assemble the layer's result from its samples."""
    return L0Score(samples=tuple(samples))


def _thousands(tokens: int) -> str:
    """Render a token count the way a record reads it: 8192 -> 8k, 1500 -> 1500."""
    if tokens >= 1024 and tokens % 1024 == 0:
        return f"{tokens // 1024}k"
    return str(tokens)


# ----------------------------------------------------------------- the report

OVERLAP_WARNING = (
    "A range is the spread across passes of the identical suite, not an error "
    "bar. Two ranges that overlap have not shown a difference, however much "
    "the ends moved."
)
"""What a reader has to know before comparing two columns.

Learned the expensive way: a set of cases that failed every pass before a
change and one pass after it reads as an improvement and is not one.
"""

DIRTY_WARNING = (
    "`*` marks a column with a record taken from an uncommitted tree: a working "
    "note, not a result."
)
"""Why a column can carry a commit and still not be comparable."""


class ReportRow(BaseModel):
    """One record, reduced to what a report needs.

    A record carries a whole run; a report compares runs, so it needs the
    identity, the numbers, and which units passed. Built by whoever read the
    file, because nothing in this module reads anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    layer: str
    date: str
    commit: str
    served: str = Field(description="The model the record was taken on.")
    quantization: str = ""
    dirty: bool = False
    by_hand: bool = Field(
        default=False,
        description="Whether the record says it was measured by hand rather than by "
        "the runner. Those predate the runner, were taken in configurations that "
        "differ from each other, and are not passes of anything.",
    )
    context: dict[str, str] = Field(
        default_factory=dict,
        description="Whatever else the record said about itself in words - the device, "
        "the backend, the note. What distinguishes two hand-taken records is in here, "
        "so listing them without it would invent a comparison.",
    )
    metrics: dict[str, float] = Field(default_factory=dict)
    units: dict[str, bool] = Field(
        default_factory=dict,
        description="Each task or case id and whether it passed. Empty for a layer "
        "that measures rather than grades, which is L0.",
    )


def report_markdown(
    rows: Sequence[ReportRow],
    *,
    title: str = "Benchmark records",
    intro: str = "",
    charts: bool = False,
) -> str:
    """Render the committed records as Markdown: one section per suite and model.

    A column is one harness commit and a cell is the range across its passes,
    which is the comparison this project actually makes - "did this change move
    the number, against the spread of changing nothing". Doing it by hand from
    terminal output is how three passes of L2 came to be described from memory
    after their record had been deleted.

    Args:
        rows: the records, in the order they were written.
        title: the page's heading, so a docs page can be the same renderer.
        intro: what to say before the first table, already Markdown.
        charts: whether to draw each metric as well as tabulate it. On for a
            page, off for a terminal, where an `<svg>` element is noise.

    Returns:
        Markdown, or a sentence saying there is nothing to report.
    """
    if not rows:
        return "No benchmark records. `nih bench --layer L1` writes one per pass."
    sections: list[str] = [f"# {title}", ""]
    if intro:
        sections += [intro.strip(), ""]
    sections += [OVERLAP_WARNING, ""]
    for (layer, served, quant), group in _grouped(rows).items():
        heading = f"{layer} on {served}" + (f" ({quant})" if quant else "")
        sections += [f"## {heading}", ""]
        measured = [row for row in group if not row.by_hand]
        columns = _columns(measured)
        sections += _table("Metric", _metric_cells(columns), columns)
        if charts:
            sections += _strips(columns)
        unit_cells = _unit_cells(columns)
        if unit_cells:
            sections += ["Per unit, how many of that column's passes it passed:", ""]
            sections += _table("Case or task", unit_cells, columns)
        if any(row.dirty for row in measured):
            sections += [DIRTY_WARNING, ""]
        sections += _by_hand([row for row in group if row.by_hand])
    return "\n".join(sections).rstrip() + "\n"


def _strips(columns: Mapping[str, Sequence[ReportRow]]) -> list[str]:
    """Draw every metric that varies, labelled, in a wrapping row of strips."""
    drawings: list[str] = []
    for metric in _metric_cells(columns):
        series = [
            (commit, [row.metrics[metric] for row in passes if metric in row.metrics])
            for commit, passes in columns.items()
        ]
        if not _worth_drawing([pair for pair in series if pair[1]]):
            continue
        svg = range_strip_svg(metric, series)
        if svg:
            drawings.append(
                f'<figure class="nih-strip"><figcaption>{_escaped(metric)}'
                f"</figcaption>{svg}</figure>"
            )
    if not drawings:
        return []
    return ['<div class="nih-strips">', *drawings, "</div>", ""]


def _by_hand(rows: Sequence[ReportRow]) -> list[str]:
    """List the records that were taken by hand, one per line and never as a range.

    They predate the runner and were taken in configurations that differ from
    each other, so folding them into a column would report a spread across
    `cpu` and `gpu` as though it were the same measurement repeated. The
    numbers are kept because they are the only ones that exist for those
    models; the comparison is withheld because there is none to make.
    """
    if not rows:
        return []
    lines = [
        "Taken by hand, before the runner existed. Not runner output, not passes "
        "of one suite, and not compared: each was a different configuration.",
        "",
    ]
    shared = _shared(rows)
    if shared:
        lines += [", ".join(f"{key}: {value}" for key, value in sorted(shared.items())), ""]
    for row in rows:
        words = ", ".join(
            f"{key} {value}" for key, value in sorted(row.context.items()) if key not in shared
        )
        numbers = ", ".join(
            f"`{name}` {_figure(value)}" for name, value in sorted(row.metrics.items())
        )
        detail = f" - {words}" if words else ""
        lines.append(f"- `{row.commit}` {row.date}{detail}: {numbers}")
    return [*lines, ""]


def _shared(rows: Sequence[ReportRow]) -> dict[str, str]:
    """Return the context every one of these records agrees on.

    Said once rather than on every line: what the backend was and that these
    were taken by hand is true of all of them, and what distinguishes them is
    what is left after it is removed.
    """
    if not rows:
        return {}
    first = rows[0].context
    return {
        key: value
        for key, value in first.items()
        if all(row.context.get(key) == value for row in rows[1:])
    }


def _grouped(
    rows: Sequence[ReportRow],
) -> dict[tuple[str, str, str], list[ReportRow]]:
    """Group records by what makes two of them comparable at all."""
    groups: dict[tuple[str, str, str], list[ReportRow]] = {}
    for row in rows:
        groups.setdefault((row.layer, row.served, row.quantization), []).append(row)
    return dict(sorted(groups.items()))


def _columns(group: Sequence[ReportRow]) -> dict[str, list[ReportRow]]:
    """Group one section's records by harness commit, first appearance first."""
    columns: dict[str, list[ReportRow]] = {}
    for row in group:
        columns.setdefault(row.commit, []).append(row)
    return columns


def _heading(commit: str, passes: Sequence[ReportRow]) -> str:
    """Name a column: the commit, its date, how many passes, and whether it is dirty."""
    count = len(passes)
    dirty = "*" if any(row.dirty for row in passes) else ""
    plural = "pass" if count == 1 else "passes"
    return f"`{commit}`{dirty} {passes[0].date}, {count} {plural}"


def _table(
    corner: str,
    cells: dict[str, dict[str, str]],
    columns: Mapping[str, Sequence[ReportRow]],
) -> list[str]:
    """Render one Markdown table: a row per name, a column per commit."""
    if not cells:
        return []
    headings = [_heading(commit, passes) for commit, passes in columns.items()]
    lines = [
        "| " + " | ".join([corner, *headings]) + " |",
        "|" + "---|" * (len(headings) + 1),
    ]
    for name, by_commit in cells.items():
        row = [by_commit.get(commit, "-") for commit in columns]
        lines.append("| " + " | ".join([f"`{name}`", *row]) + " |")
    return [*lines, ""]


def _metric_cells(
    columns: Mapping[str, Sequence[ReportRow]],
) -> dict[str, dict[str, str]]:
    """For every metric any column recorded, its range in each column."""
    cells: dict[str, dict[str, str]] = {}
    for commit, passes in columns.items():
        for row in passes:
            for metric in row.metrics:
                cells.setdefault(metric, {})
        for metric, by_commit in cells.items():
            values = [row.metrics[metric] for row in passes if metric in row.metrics]
            if values:
                by_commit[commit] = _range(values)
    return cells


def _unit_cells(
    columns: Mapping[str, Sequence[ReportRow]],
) -> dict[str, dict[str, str]]:
    """For every task or case any column graded, how many of its passes it passed."""
    cells: dict[str, dict[str, str]] = {}
    for commit, passes in columns.items():
        for row in passes:
            for unit in row.units:
                cells.setdefault(unit, {})
        for unit, by_commit in cells.items():
            graded = [row.units[unit] for row in passes if unit in row.units]
            if graded:
                by_commit[commit] = f"{sum(graded)}/{len(graded)}"
    return cells


def _range(values: Sequence[float]) -> str:
    """Render a metric across passes: one number, or the spread it covered."""
    low, high = min(values), max(values)
    if low == high:
        return _figure(low)
    return f"{_figure(low)} - {_figure(high)}"


def _figure(value: float) -> str:
    """Render a number without trailing zeros, and never as a bare integer.

    `1` and `1.0` read differently for a ratio: the second says the scale it
    is on.
    """
    text = f"{value:.4f}".rstrip("0")
    return f"{text}0" if text.endswith(".") else text


# ---------------------------------------------------------------- the charts

CHART_WIDTH = 340
"""Pixels across, chosen to sit two-up in the docs theme's content column."""

CHART_HEIGHT = 64
"""Pixels down: tall enough to read a spread, short enough to scan twelve."""

CHART_PAD = 8
"""Breathing room inside the viewBox, so a dot at the top is not clipped."""


def range_strip_svg(
    name: str,
    columns: Sequence[tuple[str, Sequence[float]]],
) -> str:
    """Draw one metric across commits: a bar per column from its low to its high.

    The drawing exists to make one thing obvious that a table makes you work
    for - two columns whose spreads overlap have not shown a difference. A bar
    that covers the same band as its neighbour says so at a glance, and this
    project has twice mistaken a moved end for a result.

    A ratio is drawn against 0 to 1, because a ratio means the same thing
    everywhere and half of it should look like half. Anything else is drawn
    against its own low and high, with **both ends labelled** - a rate whose
    three passes span 99.22 to 100.02 is a tight measurement, and on a
    zero-based axis it is a half-pixel smudge at the ceiling that says
    nothing. The labels are what stop a fitted axis reading as a big
    difference, so they are not optional decoration.

    No colour is named. Every stroke and fill is `currentColor`, so the
    drawing follows the page's own palette into dark mode instead of carrying
    a light-mode background with it.

    Args:
        name: the metric, used as the accessible label.
        columns: label and values per column, in the order to draw them.

    Returns:
        One `<svg>` element, or "" when there is nothing a drawing would add.
    """
    values = [value for _, group in columns for value in group]
    if not columns or not values:
        return ""
    high, low = max(values), min(values)
    if high <= 1.0:
        top, bottom = 1.0, 0.0
    elif high == low:
        top, bottom = high, 0.0
    else:
        top, bottom = high, low
    inner = CHART_HEIGHT - 2 * CHART_PAD
    step = (CHART_WIDTH - 2 * CHART_PAD) / len(columns)
    span = top - bottom

    def y_of(value: float) -> float:
        return CHART_PAD + inner * (1 - ((value - bottom) / span if span else 0.0))

    marks: list[str] = []
    for index, (label, group) in enumerate(columns):
        x = CHART_PAD + step * (index + 0.5)
        if not group:
            continue
        low, peak = min(group), max(group)
        marks.append(
            f'<line x1="{x:.1f}" y1="{y_of(low):.1f}" x2="{x:.1f}" y2="{y_of(peak):.1f}"'
            ' stroke="currentColor" stroke-width="7" stroke-linecap="round"'
            ' opacity="0.25" />'
        )
        marks += [
            f'<circle cx="{x:.1f}" cy="{y_of(value):.1f}" r="2.5" fill="currentColor" />'
            for value in sorted(set(group))
        ]
        marks.append(
            f'<text x="{x:.1f}" y="{CHART_HEIGHT - 1}" text-anchor="middle"'
            ' font-size="7" fill="currentColor" opacity="0.7">'
            f"{_escaped(label)}</text>"
        )
    axis = (
        f'<line x1="{CHART_PAD}" y1="{CHART_HEIGHT - CHART_PAD:.1f}"'
        f' x2="{CHART_WIDTH - CHART_PAD}" y2="{CHART_HEIGHT - CHART_PAD:.1f}"'
        ' stroke="currentColor" stroke-width="0.5" opacity="0.3" />'
    )
    scale = (
        f'<text x="2" y="{CHART_PAD + 3}" font-size="7" fill="currentColor"'
        f' opacity="0.7">{_figure(top)}</text>'
        f'<text x="2" y="{CHART_HEIGHT - CHART_PAD + 3}" font-size="7"'
        f' fill="currentColor" opacity="0.7">{_figure(bottom)}</text>'
    )
    described = ", ".join(f"{label} {_range(group)}" for label, group in columns if group)
    return (
        f'<svg viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" width="{CHART_WIDTH}"'
        f' height="{CHART_HEIGHT}" role="img" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{_escaped(name)}: {_escaped(described)}</title>"
        f"{axis}{scale}{''.join(marks)}</svg>"
    )


def _worth_drawing(columns: Sequence[tuple[str, Sequence[float]]]) -> bool:
    """Whether a chart would say anything the number beside it does not.

    One column of one value is a number, and drawing a full-height bar for it
    invents a comparison. A chart is for variation: across commits, or across
    the passes of one.
    """
    if len(columns) > 1:
        return True
    return any(len(set(group)) > 1 for _, group in columns)


def _escaped(text: str) -> str:
    """Escape the three characters that would end an element early."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

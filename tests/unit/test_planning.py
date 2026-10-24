# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The planning stage: one spec in, one plan out, and the digest bound by the harness."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from niharness.application import PlanningResult, PlanningRun, StepRunner
from niharness.application.planning import render_spec
from niharness.cli.main import app
from niharness.domain import events as ev
from niharness.domain.artifacts import (
    AcceptanceCriterion,
    Decision,
    Plan,
    PlanDraft,
    Spec,
    SpecOrigin,
    SpecVerdict,
    Task,
)
from niharness.domain.context import CompactionPolicy
from niharness.domain.control import CancelRun, Driver
from niharness.domain.events import RunOutcome
from niharness.domain.identity import RunId
from niharness.domain.markdown import render_plan
from niharness.domain.markdown import render_spec as render_spec_file
from niharness.domain.messages import ChatResponse, Usage
from niharness.domain.roles import Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig
from niharness.domain.tools import ToolName
from tests.unit.test_implement import FakeBackend, FakeControl, FakeSink, FakeTools, plan

runner = CliRunner()

RUN = RunId("20261024-090000-abc123")


def spec() -> Spec:
    return Spec(
        title="Read the clock constant from the datasheet",
        origin=SpecOrigin.HUMAN,
        goal="The driver must use the core supply voltage the erratum gives, not the table.",
        non_goals=("Rewriting the clock tree",),
        constraints=("No new dependency",),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="c1",
                statement="VDDC_VOLTS is defined once and used by the driver",
                verification="pytest tests/test_clk9000.py::test_core_voltage",
            ),
            AcceptanceCriterion(
                id="c2",
                statement="the commit body cites the erratum",
                verification="git log -1 --format=%B contains 'erratum'",
            ),
        ),
    )


def draft(*, forward_dependency: bool = False) -> PlanDraft:
    tasks = [
        Task(
            id="t1",
            title="Define the constant",
            intent="add VDDC_VOLTS with the erratum's value",
            satisfies=("c1",),
            files=("src/clk9000.py",),
        ),
        Task(
            id="t2",
            title="Cite the erratum",
            intent="explain in the commit body why the table is wrong",
            satisfies=("c2",),
            depends_on=("t3",) if forward_dependency else ("t1",),
            files=("src/clk9000.py",),
        ),
    ]
    return PlanDraft(tasks=tuple(tasks), open_questions=("Which revision of the erratum?",))


def drafted(*, forward_dependency: bool = False) -> ChatResponse:
    return ChatResponse(
        content=draft(forward_dependency=forward_dependency).model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=900, completion_tokens=200),
    )


def build(
    backend: FakeBackend, tools: FakeTools, sink: FakeSink, **kwargs: object
) -> PlanningRun:
    return PlanningRun(
        runner=StepRunner(backend=backend, tools=tools, events=sink),
        events=sink,
        tokenizer=backend,
        model="Qwen3-Coder-30B-A3B-Instruct",
        policy=CompactionPolicy(),
        **kwargs,  # type: ignore[arg-type]
    )


async def ran(
    backend: FakeBackend,
    tools: FakeTools,
    sink: FakeSink,
    *,
    control: FakeControl | None = None,
    review_spec: bool = False,
) -> PlanningResult:
    """Run the planning stage; the spec gate is off unless a test wants it.

    Off by default because most of these tests are about the plan step, and a
    gate in front of it would mean every one of them feeding the model a
    verdict it is not examining.
    """
    return await build(backend, tools, sink, control=control).run(
        spec(),
        RUN,
        branch="harness/run-x",
        profile="cpu-only",
        review_spec=review_spec,
    )


async def planned(backend: FakeBackend, tools: FakeTools, sink: FakeSink) -> Plan | None:
    return (await ran(backend, tools, sink)).plan


def gate(decision: Decision, *missing: str) -> ChatResponse:
    """The spec gate's answer."""
    return ChatResponse(
        content=SpecVerdict(
            decision=decision,
            summary="every criterion names a command" if decision is Decision.ACK else "two",
            missing=missing,
        ).model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=600, completion_tokens=90),
    )


# ------------------------------------------------------------------ the artifact


def test_the_harness_binds_the_spec_digest_not_the_model() -> None:
    bound = draft().for_spec(spec().digest())
    assert bound.spec_digest == spec().digest()
    assert bound.tasks == draft().tasks
    assert bound.open_questions == draft().open_questions


def test_a_draft_whose_dependencies_point_forward_is_not_a_plan() -> None:
    # The draft itself validates: the ordering rule lives on Plan, and binding
    # is where it is applied rather than relaxed.
    forward = draft(forward_dependency=True)
    with pytest.raises(ValueError, match="t3"):
        forward.for_spec(spec().digest())


def test_the_window_gets_the_spec_once_and_the_file_gets_it_twice() -> None:
    text = render_spec(spec())
    assert "```json" not in text, "the artifact block is for the file, not the window"
    assert "```json" in render_spec_file(spec()), "and the file does carry it"
    assert str(spec().digest()) not in text, "the digest is the harness's half"
    assert "c1" in text, "criteria are named by the ids a task must cite"
    assert "c2" in text
    assert "pytest tests/test_clk9000.py" in text, "and by how each one is verified"
    assert len(text) < len(render_spec_file(spec())), "the planner pays for one copy"


# --------------------------------------------------------------------- the stage


async def test_a_planning_run_produces_a_plan_bound_to_its_spec() -> None:
    backend, tools, sink = FakeBackend(drafted()), FakeTools(), FakeSink()
    result = await ran(backend, tools, sink)

    assert result.outcome is RunOutcome.COMPLETED
    assert result.plan is not None
    assert result.plan.spec_digest == spec().digest()
    assert [task.id for task in result.plan.tasks] == ["t1", "t2"]
    assert result.tokens > 0, "what the step spent is measured, not estimated"


async def test_the_run_records_the_spec_it_planned_and_which_gate_ran() -> None:
    backend, tools, sink = FakeBackend(drafted()), FakeTools(), FakeSink()
    await planned(backend, tools, sink)

    started = [body for body in sink.bodies if isinstance(body, ev.RunStarted)]
    accepted = [body for body in sink.bodies if isinstance(body, ev.SpecAccepted)]
    created = [body for body in sink.bodies if isinstance(body, ev.PlanCreated)]
    finished = [body for body in sink.bodies if isinstance(body, ev.RunFinished)]

    assert started[0].spec_digest == spec().digest()
    assert started[0].plan_digest is None, "a planning run is seeded by a spec"
    assert accepted[0].reviewed is False, "this run skipped the reviewer pass"
    assert created[0].task_count == 2
    assert created[0].imported is False
    assert finished[0].outcome is RunOutcome.COMPLETED


async def test_the_planner_is_offered_read_only_tools() -> None:
    backend, tools, sink = FakeBackend(drafted()), FakeTools(), FakeSink()
    await planned(backend, tools, sink)

    offered = tools.offered[0]
    assert ToolName.READ_FILE in offered
    assert ToolName.RAG_QUERY in offered
    for forbidden in (
        ToolName.GIT_COMMIT,
        ToolName.REPLACE_IN_FILE,
        ToolName.APPLY_PATCH,
        ToolName.FORMAT_CODE,
    ):
        assert forbidden not in offered, f"the planner must not be able to {forbidden}"


async def test_a_plan_that_does_not_hold_together_goes_back_to_the_human() -> None:
    backend = FakeBackend(drafted(forward_dependency=True))
    tools, sink = FakeTools(), FakeSink()
    result = await ran(backend, tools, sink)

    assert result.plan is None
    assert result.outcome is RunOutcome.FAILED
    assert "does not hold together" in result.reason
    assert "t3" in result.reason, "the reason names what the model got wrong"
    assert not [body for body in sink.bodies if isinstance(body, ev.PlanCreated)]


def test_the_plan_draft_brings_its_own_schema_and_not_the_digest() -> None:
    # The schema reaches the model on the finalising turn, where no tool is
    # offered: a schema and a tool grammar in one request is the one thing
    # llama.cpp refuses outright. What is asserted here is the schema the
    # config derives, which is what that turn then sends.
    config = LoopConfig(
        stage="plan",
        role=Role.PLANNER,
        model="m",
        allowlist=frozenset(),
        final_artifact=ArtifactKind.PLAN_DRAFT,
    )
    assert config.response_format is not None
    assert config.response_format.name == "PlanDraft"
    asked = json.dumps(config.response_format.json_schema)
    assert '"spec_digest"' not in asked, "the digest is never asked for"
    assert '"tasks"' in asked


async def test_a_planning_run_can_be_cancelled_like_any_other() -> None:
    backend, tools, sink = FakeBackend(drafted()), FakeTools(), FakeSink()
    inbox = FakeControl(CancelRun(run_id=RUN, source=Driver.CLI, reason="enough planning"))
    result = await ran(backend, tools, sink, control=inbox)

    assert result.outcome is RunOutcome.CANCELLED
    assert result.reason == "enough planning"
    assert [body for body in sink.bodies if isinstance(body, ev.RunCancelled)]


# ----------------------------------------------------------------------- the CLI


def test_plan_approve_records_the_decision_by_digest(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text(render_plan(plan()), encoding="utf-8")
    result = runner.invoke(app, ["plan", "approve", str(target), "-C", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["plan_digest"] == str(plan().digest())
    approved = Path(payload["approved"])
    assert approved.is_file()
    assert render_plan(plan()) == approved.read_text(encoding="utf-8")


def test_plan_approve_refuses_what_is_not_a_plan(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text("# Plan\n\nWe will see.\n", encoding="utf-8")
    result = runner.invoke(app, ["plan", "approve", str(target), "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "invalid plan" in result.output


def test_plan_approve_reports_a_digest_nothing_stored(tmp_path: Path) -> None:
    result = runner.invoke(app, ["plan", "approve", "-C", str(tmp_path), "--digest", "f" * 64])
    assert result.exit_code == 1
    assert "no stored plan" in result.output


def test_planning_refuses_a_spec_whose_criteria_are_not_testable(tmp_path: Path) -> None:
    loose = tmp_path / "SPEC.md"
    loose.write_text(
        json.dumps(
            {
                "title": "Make it better",
                "origin": "human",
                "goal": "improve things",
                "acceptance_criteria": [
                    {"id": "c1", "statement": "it works", "verification": "works"},
                ],
            },
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["plan", str(loose), "-C", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "not testable" in payload["detail"]


def test_run_refuses_to_implement_a_plan_the_planner_guessed_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nih run` removes exactly one stop, and this is the plan that stop was for."""
    from niharness.application import PlanningResult
    from niharness.cli import pipeline as pipeline_module
    from niharness.domain.events import StopReason

    guessed = draft().for_spec(spec().digest())
    assert guessed.open_questions, "the fixture exists to carry one"

    async def planned_anyway(*args: object, **kwargs: object) -> PlanningResult:
        return PlanningResult(
            run_id=RUN,
            outcome=RunOutcome.COMPLETED,
            stop_reason=StopReason.FINAL_ANSWER,
            tool_calls=3,
            tokens=900,
            plan=guessed,
            reason="2 task(s) planned",
        )

    monkeypatch.setattr(pipeline_module.plan_module, "execute", planned_anyway)
    (tmp_path / "SPEC.md").write_text(render_spec_file(spec()), encoding="utf-8")
    result = runner.invoke(
        app, ["run", str(tmp_path / "SPEC.md"), "-C", str(tmp_path), "--json"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["open_questions"] == list(guessed.open_questions)
    assert "--ignore-open-questions" in payload["error"]
    assert Path(payload["approved"]).is_file(), "the plan is approved and ready to implement"


# ------------------------------------------------------------------ the spec gate


async def test_an_acked_spec_is_planned_and_the_event_says_it_was_reviewed() -> None:
    backend = FakeBackend(gate(Decision.ACK), drafted())
    tools, sink = FakeTools(), FakeSink()
    result = await ran(backend, tools, sink, review_spec=True)

    assert result.plan is not None
    assert result.spec_verdict is not None
    assert result.spec_verdict.decision is Decision.ACK
    accepted = [body for body in sink.bodies if isinstance(body, ev.SpecAccepted)]
    assert accepted[0].reviewed is True


async def test_a_naked_spec_is_never_planned_and_the_reason_is_actionable() -> None:
    refusal = gate(Decision.NAK, "c2 cannot fail: `git log` would match any commit")
    backend = FakeBackend(refusal, drafted())
    tools, sink = FakeTools(), FakeSink()
    result = await ran(backend, tools, sink, review_spec=True)

    assert result.plan is None
    assert result.outcome is RunOutcome.FAILED
    assert "cannot fail" in result.reason, "the author is told what to change"
    assert not [body for body in sink.bodies if isinstance(body, ev.SpecAccepted)]
    assert not [body for body in sink.bodies if isinstance(body, ev.PlanCreated)]
    assert len(backend.requests) == 1, "the plan step never ran"


async def test_the_spec_gate_gets_no_tools() -> None:
    backend = FakeBackend(gate(Decision.ACK), drafted())
    tools, sink = FakeTools(), FakeSink()
    await ran(backend, tools, sink, review_spec=True)

    assert tools.offered[0] == frozenset(), "a document needs no tools to read"
    assert ToolName.READ_FILE in tools.offered[1], "the planner still gets them"


async def test_the_spec_gate_reads_the_gates_own_prompt_not_the_reviewers() -> None:
    backend = FakeBackend(gate(Decision.ACK), drafted())
    tools, sink = FakeTools(), FakeSink()
    await ran(backend, tools, sink, review_spec=True)

    system = backend.requests[0].messages[0].content
    assert "spec gate" in system
    assert "cannot fail" in system, "the defect it exists to find is named"
    assert "run_build" not in system, "the reviewer's instructions are about a commit"


def test_a_refused_spec_has_to_say_what_is_missing() -> None:
    with pytest.raises(ValueError, match="name what is missing"):
        SpecVerdict(decision=Decision.NAK, summary="not good enough")
    with pytest.raises(ValueError, match="cannot also be missing"):
        SpecVerdict(decision=Decision.ACK, summary="fine", missing=("something",))

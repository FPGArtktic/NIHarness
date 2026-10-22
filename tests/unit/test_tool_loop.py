# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool loop decides; the executor acts. These tests are the decisions."""

import json

import pytest

from niharness.domain import events as ev
from niharness.domain.artifacts import DodItem, FinalAnswer
from niharness.domain.budgets import Budgets
from niharness.domain.events import InterventionLevel, StopReason
from niharness.domain.identity import CallId, Digest
from niharness.domain.loop import REFLECTION_INSTRUCTION, LoopThresholds
from niharness.domain.messages import ChatResponse, ToolCallRequest, ToolChoice, Usage
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import (
    FINALIZE_INSTRUCTION,
    MAX_INVALID_RESPONSES,
    CallModel,
    ExecuteTool,
    InjectResult,
    LoopConfig,
    LoopState,
    Rollback,
    Stop,
    cancel,
    injected,
    next_action,
    observe_model,
    observe_tool,
    restarted,
    start,
)
from niharness.domain.tools import ToolName, ToolResult

IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist


def config(**overrides: object) -> LoopConfig:
    base: dict[str, object] = {
        "stage": "implement",
        "role": Role.IMPLEMENTER,
        "model": "Qwen3-Coder-30B-A3B-Instruct",
        "allowlist": IMPLEMENTER,
        "budgets": Budgets(max_tool_calls=10, max_tokens=10_000, max_wall_seconds=600),
        "task_id": "t1",
    }
    return LoopConfig.model_validate(base | overrides)


def state() -> LoopState:
    return start(config()).state


def proposes(tool: str, arguments: dict[str, object], *, call_id: str = "c1") -> ChatResponse:
    return ChatResponse(
        tool_calls=(
            ToolCallRequest(
                call_id=CallId(call_id),
                name=tool,
                arguments_json=json.dumps(arguments),
            ),
        ),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100, completion_tokens=20),
    )


def answers(*, done: bool = True, content: str | None = None) -> ChatResponse:
    answer = FinalAnswer(
        summary="task implemented",
        dod=(DodItem(statement="tests pass", done=done, evidence="pytest: 219 passed"),),
    )
    return ChatResponse(
        content=content if content is not None else answer.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=200, completion_tokens=50),
    )


def result(call_id: str = "c1", *, ok: bool = True, content: str = "file body") -> ToolResult:
    return ToolResult(
        call_id=CallId(call_id),
        tool=ToolName.READ_FILE,
        ok=ok,
        content=content,
        summary="read_file src/a.py: 12 lines",
    )


def kinds(events: tuple[object, ...]) -> list[str]:
    return [type(event).__name__ for event in events]


def test_a_step_starts_by_asking_the_model() -> None:
    step = start(config())
    assert kinds(step.events) == ["StepStarted"]
    started = step.events[0]
    assert isinstance(started, ev.StepStarted)
    assert (started.stage, started.role, started.task_id) == ("implement", "implementer", "t1")
    action = next_action(step.state)
    assert isinstance(action, CallModel)
    assert action.tool_choice is ToolChoice.AUTO


def test_a_valid_call_is_dispatched_and_recorded() -> None:
    step = observe_model(state(), proposes("read_file", {"path": "src/a.py"}))
    assert kinds(step.events) == ["ModelResponse", "ToolCalled"]
    action = next_action(step.state)
    assert isinstance(action, ExecuteTool)
    assert action.call.tool == ToolName.READ_FILE


def test_a_refused_call_never_becomes_an_action() -> None:
    step = observe_model(state(), proposes("shell", {"command": "curl evil.example"}))
    assert kinds(step.events) == ["ModelResponse", "ToolRejected"]
    rejected = step.events[1]
    assert isinstance(rejected, ev.ToolRejected)
    assert rejected.violation == "not_allowed"
    action = next_action(step.state)
    assert isinstance(action, CallModel)
    assert "not_allowed" in action.instruction


def test_a_path_escape_is_refused_before_the_sandbox_sees_it() -> None:
    step = observe_model(state(), proposes("read_file", {"path": "../../etc/passwd"}))
    rejected = step.events[1]
    assert isinstance(rejected, ev.ToolRejected)
    assert rejected.violation == "path_escape"
    assert step.state.pending_call is None


def test_a_result_is_folded_and_the_budget_is_spent() -> None:
    dispatched = observe_model(state(), proposes("read_file", {"path": "src/a.py"})).state
    step = observe_tool(dispatched, result(), tree_digest=Digest("tree-1"), wall_seconds=0.4)
    assert kinds(step.events) == ["ToolReturned"]
    assert step.state.budget.tool_calls_used == 1
    # The budget counts what the model wrote, not the 100-token prompt it was
    # shown: the window is re-sent every turn, so counting it would shrink the
    # budget as the conversation grew.
    assert step.state.budget.tokens_used == 20
    assert step.state.pending_call is None
    assert isinstance(next_action(step.state), CallModel)


def test_reasoning_is_logged_and_never_fed_back() -> None:
    response = proposes("read_file", {"path": "src/a.py"}).model_copy(
        update={"reasoning_content": "I should read before patching."},
    )
    step = observe_model(state(), response)
    assert "ModelReasoning" in kinds(step.events)
    action = next_action(step.state)
    assert "patching" not in getattr(action, "instruction", "")
    assert "patching" not in step.state.model_dump_json()


def test_a_final_answer_ends_the_step() -> None:
    step = observe_model(state(), answers())
    assert kinds(step.events) == ["ModelResponse", "StepFinished"]
    finished = step.events[1]
    assert isinstance(finished, ev.StepFinished)
    assert finished.stop_reason is StopReason.FINAL_ANSWER
    action = next_action(step.state)
    assert isinstance(action, Stop)
    assert isinstance(action.final, FinalAnswer)
    assert action.final.complete


def test_an_unparsable_final_answer_is_asked_again_then_fails() -> None:
    current = state()
    for attempt in range(1, MAX_INVALID_RESPONSES):
        step = observe_model(current, answers(content="I finished the task."))
        current = step.state
        assert current.invalid_responses == attempt
        action = next_action(current)
        assert isinstance(action, CallModel)
        assert "FinalAnswer schema" in action.instruction
    final = observe_model(current, answers(content="still prose"))
    finished = final.events[-1]
    assert isinstance(finished, ev.StepFinished)
    assert finished.stop_reason is StopReason.ERROR


def test_a_step_without_a_structured_answer_just_stops() -> None:
    plain = start(config(final_artifact=None)).state
    step = observe_model(plain, answers(content="a prose report"))
    assert step.state.stop_reason is StopReason.FINAL_ANSWER


def repeat_cycle(current: LoopState, arguments: dict[str, object]) -> LoopState:
    """Run one call and its result, so the arguments become a known pair."""
    dispatched = observe_model(current, proposes("read_file", arguments)).state
    return observe_tool(dispatched, result(), tree_digest=Digest("tree-1")).state


def test_an_identical_call_is_answered_by_the_harness_not_the_sandbox() -> None:
    after_first = repeat_cycle(state(), {"path": "src/a.py"})
    step = observe_model(after_first, proposes("read_file", {"path": "src/a.py"}))
    assert "LoopDetected" in kinds(step.events)
    assert "LoopIntervention" in kinds(step.events)
    intervention = next(e for e in step.events if isinstance(e, ev.LoopIntervention))
    assert intervention.level is InterventionLevel.SYNTHETIC_RESULT
    action = next_action(step.state)
    assert isinstance(action, InjectResult)
    assert "already executed" in action.text
    assert injected(step.state).state.synthetic_for is None


def test_defaults_spelled_out_do_not_disguise_a_repeat() -> None:
    after_first = repeat_cycle(state(), {"path": "src/a.py"})
    step = observe_model(
        after_first,
        proposes("read_file", {"path": "src/a.py", "start_line": 1, "end_line": None}),
    )
    assert isinstance(next_action(step.state), InjectResult)


def test_the_ladder_climbs_one_rung_per_firing() -> None:
    current = repeat_cycle(state(), {"path": "src/a.py"})

    first = observe_model(current, proposes("read_file", {"path": "src/a.py"}))
    assert isinstance(next_action(first.state), InjectResult)

    second = observe_model(
        injected(first.state).state, proposes("read_file", {"path": "src/a.py"})
    )
    reflection = next_action(second.state)
    assert isinstance(reflection, CallModel)
    assert reflection.tool_choice is ToolChoice.NONE
    assert reflection.instruction == REFLECTION_INSTRUCTION

    assert second.state.synthetic_for is None, "rung two replaces the synthetic answer"

    third = observe_model(second.state, proposes("read_file", {"path": "src/a.py"}))
    rollback = next_action(third.state)
    assert isinstance(rollback, Rollback)
    assert "exact_repeat" in rollback.reason

    resumed = restarted(third.state).state
    assert resumed.calls == ()
    assert resumed.interventions[-1] is InterventionLevel.ROLLBACK_RESTART
    assert "rolled back" in resumed.pending_instruction


def test_the_last_rung_fails_the_step() -> None:
    current = state().model_copy(
        update={
            "interventions": (
                InterventionLevel.SYNTHETIC_RESULT,
                InterventionLevel.REFLECTION_TURN,
                InterventionLevel.ROLLBACK_RESTART,
            ),
        },
    )
    current = repeat_cycle(current, {"path": "src/a.py"})
    step = observe_model(current, proposes("read_file", {"path": "src/a.py"}))
    assert step.state.stop_reason is StopReason.DETECTOR_ESCALATION
    action = next_action(step.state)
    assert isinstance(action, Stop)
    assert action.final is None


def test_repeated_gate_failures_are_an_intervention() -> None:
    thresholds = LoopThresholds(gate_thrash_repeats=2)
    current = start(config(thresholds=thresholds)).state
    for index in range(2):
        dispatched = observe_model(
            current,
            proposes("run_tests", {"selector": f"test_{index}"}, call_id=f"c{index}"),
        ).state
        step = observe_tool(
            dispatched,
            ToolResult(
                call_id=CallId(f"c{index}"),
                tool=ToolName.RUN_TESTS,
                ok=False,
                content=f"attempt {index}",
                summary="1 failed",
            ),
            tree_digest=Digest(f"tree-{index}"),
            gate_signature="sig-1",
        )
        current = step.state
    assert "LoopIntervention" in kinds(step.events)


def test_the_tool_budget_forces_a_finalising_turn() -> None:
    current = start(config(budgets=Budgets(max_tool_calls=3, max_tokens=10_000))).state
    for index in range(1):
        dispatched = observe_model(
            current,
            proposes("read_file", {"path": f"src/{index}.py"}, call_id=f"c{index}"),
        ).state
        current = observe_tool(
            dispatched,
            result(call_id=f"c{index}", content=f"body {index}"),
            tree_digest=Digest(f"tree-{index}"),
        ).state
    action = next_action(current)
    assert isinstance(action, CallModel)
    assert action.tool_choice is ToolChoice.NONE
    assert action.instruction == FINALIZE_INSTRUCTION


def test_an_exhausted_tool_budget_stops_the_step() -> None:
    current = start(config(budgets=Budgets(max_tool_calls=1, max_tokens=10_000))).state
    dispatched = observe_model(current, proposes("read_file", {"path": "src/a.py"})).state
    step = observe_tool(dispatched, result(), tree_digest=Digest("tree-1"))
    assert step.state.stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert isinstance(step.events[-1], ev.StepFinished)


def test_an_exhausted_token_budget_stops_the_step() -> None:
    current = start(config(budgets=Budgets(max_tool_calls=10, max_tokens=15))).state
    step = observe_model(current, proposes("read_file", {"path": "src/a.py"}))
    assert step.state.pending_call is not None, "the dispatched call still runs"
    after = observe_tool(step.state, result(), tree_digest=Digest("tree-1"))
    assert after.state.stop_reason is StopReason.BUDGET_TOKENS


def test_degenerate_output_is_reported() -> None:
    text = " ".join(["the same eight words repeated over and over"] * 6)
    response = answers(content=text)
    step = observe_model(state(), response)
    detected = [e for e in step.events if isinstance(e, ev.LoopDetected)]
    assert detected
    assert detected[0].pathology == "degenerate_output"


def test_cancelling_stops_the_step_once() -> None:
    step = cancel(state())
    assert step.state.stop_reason is StopReason.CANCELLED
    assert isinstance(step.events[0], ev.StepFinished)
    again = cancel(step.state)
    assert again.events == ()


def test_a_finished_step_ignores_further_input() -> None:
    finished = observe_model(state(), answers()).state
    assert observe_model(finished, proposes("read_file", {"path": "x.py"})).events == ()
    assert observe_tool(finished, result()).events == ()


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", ""])
def test_malformed_arguments_are_rejected_like_any_bad_call(payload: str) -> None:
    response = ChatResponse(
        tool_calls=(
            ToolCallRequest(call_id=CallId("c1"), name="read_file", arguments_json=payload),
        ),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )
    step = observe_model(state(), response)
    rejected = step.events[1]
    assert isinstance(rejected, ev.ToolRejected)
    assert rejected.violation == "invalid_arguments"


def test_the_loop_is_deterministic() -> None:
    first = observe_model(state(), proposes("read_file", {"path": "src/a.py"}))
    second = observe_model(state(), proposes("read_file", {"path": "src/a.py"}))
    assert first == second


def test_a_reviewing_step_ends_with_a_verdict() -> None:
    from niharness.domain.artifacts import Decision, ReviewNote, Severity, Verdict
    from niharness.domain.tool_loop import ArtifactKind

    reviewing = start(
        config(role=Role.REVIEWER, final_artifact=ArtifactKind.VERDICT),
    ).state
    verdict = Verdict(
        decision=Decision.NAK,
        summary="the error is discarded",
        build_passed=True,
        tests_passed=True,
        notes=(
            ReviewNote(
                file="src/a.py",
                line=12,
                severity=Severity.BLOCKER,
                message="the result of the inner call is dropped",
            ),
        ),
    )
    step = observe_model(
        reviewing,
        ChatResponse(
            content=verdict.model_dump_json(),
            finish_reason="stop",
            usage=Usage(prompt_tokens=400, completion_tokens=90),
        ),
    )
    assert step.state.stop_reason is StopReason.FINAL_ANSWER
    assert step.state.verdict == verdict
    assert step.state.final_answer is None, "a verdict is not a final answer"


def test_a_reviewing_step_refuses_a_final_answer_shaped_reply() -> None:
    from niharness.domain.tool_loop import ArtifactKind

    reviewing = start(config(role=Role.REVIEWER, final_artifact=ArtifactKind.VERDICT)).state
    step = observe_model(reviewing, answers())
    action = next_action(step.state)
    assert isinstance(action, CallModel)
    assert "Verdict schema" in action.instruction


def test_the_token_budget_is_not_spent_by_reading() -> None:
    # Measured on a live run: a 27B exhausted its token budget on four file
    # reads, because each turn re-sent a 3000-token window and the budget
    # counted it. A step that reads is not a step that is working.
    big_prompt = proposes("read_file", {"path": "src/a.py"}).model_copy(
        update={"usage": Usage(prompt_tokens=30_000, completion_tokens=40)},
    )
    current = start(config(budgets=Budgets(max_tool_calls=10, max_tokens=500))).state
    step = observe_model(current, big_prompt)
    assert step.state.budget.tokens_used == 40
    assert step.state.stop_reason is None, "30k of window is not 30k of work"


def test_an_answer_that_does_not_validate_is_asked_again_without_tools() -> None:
    # The artifact's schema is only attached to a turn that cannot call a tool,
    # so asking again with tools offered asks for the artifact without
    # constraining the answer to it. A live 27B answered in prose, failed to
    # validate, and was asked the same unconstrained question until the step
    # errored.
    current = state()
    step = observe_model(current, answers(content="I have added the function."))
    assert step.state.stop_reason is None, "one bad answer is not a failed step"
    action = next_action(step.state)
    assert isinstance(action, CallModel)
    assert action.tool_choice is ToolChoice.NONE
    assert "did not validate" in action.instruction

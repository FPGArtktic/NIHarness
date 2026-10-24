# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool loop decides; the executor acts. These tests are the decisions."""

import json

import pytest

from niharness.domain import events as ev
from niharness.domain.artifacts import (
    Decision,
    DodItem,
    FinalAnswer,
    ReviewNote,
    Severity,
    Verdict,
)
from niharness.domain.budgets import Budgets
from niharness.domain.events import InterventionLevel, StopReason
from niharness.domain.identity import CallId, Digest
from niharness.domain.loop import REFLECTION_INSTRUCTION, CallRecord, LoopThresholds
from niharness.domain.messages import (
    ChatResponse,
    ResponseFormat,
    ToolCallRequest,
    ToolChoice,
    Usage,
)
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import (
    FINALIZE_INSTRUCTION,
    MAX_INVALID_RESPONSES,
    ArtifactKind,
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


def test_degenerate_output_is_refused_and_not_just_reported() -> None:
    """The rule says abort generation; the code used to only log the finding.

    It then dispatched the call anyway, and nothing downstream survives that:
    the arguments arrive truncated mid-string, the window carries the junk, and
    the next tokenizer call fails on it - which ended a benchmark run rather
    than a step.
    """
    text = " ".join(["the same eight words repeated over and over"] * 6)
    step = observe_model(state(), answers(content=text))
    detected = [e for e in step.events if isinstance(e, ev.LoopDetected)]
    assert detected
    assert detected[0].pathology == "degenerate_output"
    assert step.state.final is None, "the answer was not accepted"
    assert step.state.pending_call is None, "and nothing was dispatched"
    assert "repeated itself" in step.state.pending_instruction
    assert step.state.calls[-1].invalid, "it counts against the invalid-call detector"


def test_a_repetition_inside_the_tool_arguments_is_caught_too() -> None:
    """Checking the prose alone misses the shape this actually takes.

    Observed on a live run: a 4B emitted `last_retry_time_ms_ns_ns_ns...`
    growing inside a `replace_in_file` argument until the tool call's JSON was
    truncated mid-string. The prose was empty, so a check on `content` saw
    nothing at all.
    """
    # The real one, trimmed: a dataclass field repeated until the JSON was cut.
    runaway = "\n".join(["    last_retry_time_ms_ns: Optional[int] = None"] * 10)
    step = observe_model(
        state(), proposes("replace_in_file", {"path": "a.py", "find": runaway})
    )
    detected = [e for e in step.events if isinstance(e, ev.LoopDetected)]
    assert detected
    assert detected[0].pathology == "degenerate_output"
    assert step.state.pending_call is None, "the call never reaches the sandbox"


def test_a_long_answer_that_does_not_repeat_is_left_alone() -> None:
    """The check is repetition, not length."""
    step = observe_model(
        state(),
        proposes(
            "apply_patch", {"diff": "\n".join(f"+line {n} of a real patch" for n in range(80))}
        ),
    )
    assert not [e for e in step.events if isinstance(e, ev.LoopDetected)]
    assert step.state.pending_call is not None


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


def reviewing_verdict(*, tests_passed: bool = True) -> Verdict:
    return Verdict(
        decision=Decision.NAK,
        summary="the error is discarded",
        build_passed=False,
        tests_passed=tests_passed,
        notes=(
            ReviewNote(
                file="src/a.py",
                line=12,
                severity=Severity.BLOCKER,
                message="the result of the inner call is dropped",
            ),
        ),
    )


def said(verdict: Verdict) -> ChatResponse:
    return ChatResponse(
        content=verdict.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=400, completion_tokens=90),
    )


def reviewing_state() -> LoopState:
    return start(config(role=Role.REVIEWER, final_artifact=ArtifactKind.VERDICT)).state


def test_a_reviewing_step_ends_with_a_verdict() -> None:
    verdict = reviewing_verdict(tests_passed=False)
    step = observe_model(reviewing_state(), said(verdict))
    assert step.state.stop_reason is StopReason.FINAL_ANSWER
    assert step.state.verdict == verdict


def test_a_verdict_claiming_the_tests_passed_is_sent_back_once() -> None:
    """The reviewer exists because a claim is not evidence. So does this check."""
    claimed = reviewing_verdict()
    step = observe_model(reviewing_state(), said(claimed))

    assert step.state.stop_reason is None, "the step does not end on an unearned claim"
    assert step.state.verdict is None
    assert "run_tests was never called" in step.state.pending_instruction
    assert step.state.pending_tool_choice is ToolChoice.AUTO, "it can go and run them"
    intervened = [body for body in step.events if isinstance(body, ev.LoopIntervention)]
    assert intervened[0].pathology == "unbacked_claim"
    assert "run_tests" in intervened[0].detail


def test_a_verdict_backed_by_a_green_run_stands() -> None:
    ran = reviewing_state().model_copy(
        update={
            "calls": (
                CallRecord(
                    seq=1,
                    tool="run_tests",
                    arguments_digest=Digest("a" * 16),
                    result_digest=Digest("b" * 16),
                ),
            ),
        },
    )
    verdict = reviewing_verdict()
    step = observe_model(ran, said(verdict))
    assert step.state.stop_reason is StopReason.FINAL_ANSWER
    assert step.state.verdict == verdict


def test_a_verdict_backed_only_by_a_red_run_is_sent_back() -> None:
    red = reviewing_state().model_copy(
        update={
            "calls": (
                CallRecord(
                    seq=1,
                    tool="run_tests",
                    arguments_digest=Digest("a" * 16),
                    result_digest=Digest("b" * 16),
                    failed=True,
                ),
            ),
        },
    )
    step = observe_model(red, said(reviewing_verdict()))
    assert step.state.stop_reason is None
    assert "came back red" in step.state.pending_instruction


def test_the_evidence_is_asked_for_once_and_then_the_verdict_stands() -> None:
    """A second refusal would be a step that cannot end, which is a wall."""
    asked = reviewing_state().model_copy(update={"evidence_asked": True})
    verdict = reviewing_verdict()
    step = observe_model(asked, said(verdict))
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


def test_a_step_that_demands_an_artifact_is_given_its_schema() -> None:
    """The two defaults used to disagree, and the step could not end.

    `final_artifact` defaults to `final_answer` while `response_format` was
    None, so a caller setting neither asked for an artifact whose schema was
    never sent - the schema is only attached on the turn without tools - and
    the loop rejected every prose answer until the step errored. Found by
    building exactly that config for `nih bench`.
    """
    config = LoopConfig(
        stage="bench",
        role=Role.IMPLEMENTER,
        model="m",
        allowlist=DEFAULT_ROLES[Role.IMPLEMENTER].allowlist,
    )
    assert config.final_artifact is ArtifactKind.FINAL_ANSWER
    assert config.response_format is not None
    assert config.response_format.name == ArtifactKind.FINAL_ANSWER.value
    assert "properties" in config.response_format.json_schema


def test_a_step_that_wants_no_artifact_is_given_no_schema() -> None:
    config = LoopConfig(
        stage="bench",
        role=Role.IMPLEMENTER,
        model="m",
        allowlist=DEFAULT_ROLES[Role.IMPLEMENTER].allowlist,
        final_artifact=None,
    )
    assert config.response_format is None


def test_a_caller_that_brings_its_own_schema_keeps_it() -> None:
    """A different name, or a grammar beside it, is the caller's business."""
    mine = ResponseFormat(name="mine", json_schema={"type": "object"}, grammar='root ::= "x"')
    config = LoopConfig(
        stage="bench",
        role=Role.IMPLEMENTER,
        model="m",
        allowlist=DEFAULT_ROLES[Role.IMPLEMENTER].allowlist,
        response_format=mine,
    )
    assert config.response_format == mine


def test_a_step_can_run_out_of_clock() -> None:
    """`max_wall_seconds` is a stop condition §4 lists and nothing could reach.

    The clock is charged where the time goes: inference in `observe_model`,
    tool execution in `observe_tool`. Both take the duration as an argument,
    because the domain reads no clock - and until the caller passed one, the
    guard against a hang was a number nothing compared anything to.
    """
    short = start(
        config(budgets=Budgets(max_tool_calls=10, max_tokens=10_000, max_wall_seconds=30))
    ).state
    step = observe_model(short, proposes("read_file", {"path": "src/a.py"}), seconds=12.0)
    assert step.state.budget.wall_seconds_used == 12.0
    assert step.state.stop_reason is None, "twelve of thirty seconds is not exhausted"

    spent = observe_tool(step.state, result(), wall_seconds=25.0)
    assert spent.state.stop_reason is StopReason.BUDGET_WALL_TIME
    returned = next(body for body in spent.events if isinstance(body, ev.ToolReturned))
    assert returned.duration_seconds == 25.0, "and the log says where the time went"

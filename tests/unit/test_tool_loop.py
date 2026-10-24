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


def answers_complete() -> ChatResponse:
    """A final answer whose checklist is fully ticked."""
    answer = FinalAnswer(
        summary="debits added alongside credits",
        dod=(
            DodItem(statement="implemented", done=True, evidence="replace_in_file"),
            DodItem(statement="committed in one commit", done=True, evidence="git_commit"),
        ),
    )
    return ChatResponse(
        content=answer.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=200, completion_tokens=50),
    )


def committing(*, green: bool = True) -> LoopState:
    """A state in which a `git_commit` call has been executed."""
    state = observe_model(
        start(config(requires_commit=True)).state,
        proposes(
            "git_commit",
            {
                "message": "feat: add debits alongside credits\n\n"
                "A caller reconciling a statement needs the money out as well as "
                "the money in.",
            },
        ),
    ).state
    return observe_tool(state, result(ok=green)).state


def test_a_step_that_says_done_without_committing_is_asked_once() -> None:
    """The implementer is held to the standard the reviewer is held to.

    Measured on L3: the step ended with a complete checklist, `git_commit` was
    never called, the commit gate found nothing landed, and the fix loop burned
    its revisions on work the hidden tests already accepted.
    """
    edited = observe_tool(
        observe_model(
            start(config(requires_commit=True)).state,
            proposes("replace_in_file", {"path": "src/a.py", "find": "x", "replace": "y"}),
        ).state,
        result(),
    ).state
    asked = observe_model(edited, answers_complete())
    assert asked.state.stop_reason is None, "the step is not over"
    assert asked.state.commit_asked is True
    assert "git_commit" in asked.state.pending_instruction
    assert asked.state.pending_tool_choice is ToolChoice.AUTO, "it must be able to commit"
    fired = [body for body in asked.events if isinstance(body, ev.LoopIntervention)]
    assert [body.pathology for body in fired] == ["uncommitted_completion"]

    # Asked once. A second unbacked answer is accepted and the log keeps it.
    again = observe_model(asked.state, answers_complete())
    assert again.state.stop_reason is StopReason.FINAL_ANSWER


def test_a_committed_step_is_left_alone() -> None:
    answered = observe_model(committing(), answers_complete())
    assert answered.state.stop_reason is StopReason.FINAL_ANSWER
    assert answered.state.commit_asked is False


def test_a_red_gate_is_not_a_commit() -> None:
    answered = observe_model(committing(green=False), answers_complete())
    assert answered.state.stop_reason is None, "nothing landed, so the claim is unearned"
    assert "came back red" in answered.state.pending_instruction


def test_an_incomplete_checklist_is_asked_about_the_commit_too() -> None:
    """Saying "I am not done" was taken as evidence, and it is not evidence.

    The exemption was written on the reading that a model leaving items
    unticked is weighing them. Fourteen L3 passes say otherwise: all ten
    Definition of Done items appear in the unticked list about equally often,
    which is a checklist returned false wholesale (ADR 0030).
    """
    answered = observe_model(start(config(requires_commit=True)).state, answers(done=False))
    assert answered.state.stop_reason is None, "asked once, while there is budget"
    assert answered.state.commit_asked is True
    assert "git_commit" in answered.state.pending_instruction

    # And still only once, whatever the second answer says.
    again = observe_model(answered.state, answers(done=False))
    assert again.state.stop_reason is StopReason.FINAL_ANSWER


def test_a_step_that_owes_no_commit_is_never_asked_for_one() -> None:
    """An L1 task that edits a file to prove it can has finished when it has."""
    answered = observe_model(state(), answers_complete())
    assert answered.state.stop_reason is StopReason.FINAL_ANSWER
    assert answered.state.commit_asked is False


def reflecting() -> LoopState:
    """A state whose next turn is the reflection turn."""
    current = repeat_cycle(state(), {"path": "src/a.py"})
    first = observe_model(current, proposes("read_file", {"path": "src/a.py"}))
    second = observe_model(
        injected(first.state).state, proposes("read_file", {"path": "src/a.py"})
    )
    assert second.state.reflecting is True
    return second.state


def test_the_reflection_turn_asks_for_prose_and_is_not_given_a_grammar() -> None:
    """Withholding tools is not the same question as demanding the artifact.

    The schema was attached whenever tools were off, and the reflection turn
    turns them off - so the model was told to answer in plain prose by a
    request whose grammar forced a `FinalAnswer`. It complied with the grammar.
    """
    action = next_action(reflecting())
    assert isinstance(action, CallModel)
    assert action.tool_choice is ToolChoice.NONE, "no tool call is accepted"
    assert action.expects_artifact is False, "and no artifact is demanded either"

    forced = start(config(budgets=Budgets(max_tool_calls=1, max_tokens=99, max_wall_seconds=9)))
    finalising = next_action(forced.state)
    assert isinstance(finalising, CallModel)
    assert finalising.expects_artifact is True, "forced finalisation does want it"


def test_the_reflections_own_words_do_not_end_the_step() -> None:
    """Measured on L3: every task a detector touched was handed back.

    The reflection's prose arrived as a `FinalAnswer` whose `dod` items were
    the model's list of attempts, every one `done=False`, so the step ended
    with an incomplete checklist and the task went to a human. The rung meant
    to get a model unstuck was terminating its task.
    """
    answered = observe_model(reflecting(), answers(done=False, content="I tried a patch."))
    assert answered.state.stop_reason is None, "the step goes on"
    assert answered.state.final is None, "and that was not its answer"
    assert answered.state.reflecting is False, "the turn happened"
    assert next_action(answered.state).tool_choice is ToolChoice.AUTO, "tools come back"


def test_a_call_proposed_on_the_reflection_turn_still_climbs_the_ladder() -> None:
    """The instruction says none is accepted; repeating one has earned the next rung."""
    third = observe_model(reflecting(), proposes("read_file", {"path": "src/a.py"}))
    assert isinstance(next_action(third.state), Rollback)

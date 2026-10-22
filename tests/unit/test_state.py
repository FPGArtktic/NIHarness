# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run state is a fold: the same stream always yields the same snapshot."""

from datetime import UTC, datetime, timedelta

import pytest

from niharness.domain import events as ev
from niharness.domain.events import Event, EventBody, InterventionLevel, RunOutcome, StopReason
from niharness.domain.identity import CallId, Digest, RunId, StepId
from niharness.domain.state import fold

RUN = RunId("0123456789abcdef")
STEP = StepId("step-1")
START = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def stream(*bodies: EventBody) -> list[Event]:
    return [
        Event(
            seq=index,
            ts=START + timedelta(seconds=index),
            run_id=RUN,
            step_id=STEP,
            body=body,
        )
        for index, body in enumerate(bodies, start=1)
    ]


def test_folding_an_empty_stream_needs_a_run_id() -> None:
    assert fold([], RUN).seq == 0
    with pytest.raises(ValueError, match="empty stream"):
        fold([])


def test_sequence_must_increase() -> None:
    events = stream(ev.RunResumed(from_seq=1), ev.RunResumed(from_seq=1))
    broken = [events[0], events[1].model_copy(update={"seq": 1})]
    with pytest.raises(ValueError, match="went backwards"):
        fold(broken)


def test_counters_accumulate_across_the_stream() -> None:
    snapshot = fold(
        stream(
            ev.RunStarted(branch="harness/run-x", profile="hybrid-small-vram", model="qwen"),
            ev.StepStarted(
                stage="implement",
                role="implementer",
                max_tool_calls=10,
                max_tokens=1_000,
                max_wall_seconds=60,
            ),
            ev.ToolCalled(
                call_id=CallId("c1"),
                name="read_file",
                arguments={"path": "a.py"},
                arguments_digest=Digest("a" * 16),
            ),
            ev.ToolRejected(name="shell", violation="not_allowed", detail="no"),
            ev.GateFinished(tier="quick", passed=False, failure_signature="sig-1"),
            ev.GateFinished(tier="quick", passed=True),
            ev.VcsCommitted(
                sha="deadbeef", subject="feat: x", files_changed=2, insertions=10, deletions=1
            ),
            ev.ContextCompacted(before_tokens=900, after_tokens=400, deterministic_only=True),
            ev.LoopIntervention(
                level=InterventionLevel.REFLECTION_TURN, pathology="exact_repeat"
            ),
            ev.StepFinished(
                stop_reason=StopReason.FINAL_ANSWER,
                tool_calls_used=1,
                tokens_used=1_234,
                wall_seconds=12.5,
            ),
        ),
    )
    assert snapshot.stage == "implement"
    assert snapshot.tool_calls == 1
    assert snapshot.rejected_calls == 1
    assert snapshot.gate_runs == 2
    assert snapshot.gate_failures == 1
    assert snapshot.commits == ("deadbeef",)
    assert snapshot.compactions == 1
    assert snapshot.highest_intervention == int(InterventionLevel.REFLECTION_TURN)
    assert snapshot.tokens_used == 1_234
    assert snapshot.step_id is None
    assert not snapshot.finished


def test_questions_open_and_close() -> None:
    snapshot = fold(
        stream(
            ev.QuestionsAsked(questions_digest=Digest("q" * 16), count=2),
            ev.QuestionsAnswered(answers_digest=Digest("a" * 16), count=2),
        ),
    )
    assert snapshot.open_questions == 0


def test_cancellation_is_terminal() -> None:
    snapshot = fold(stream(ev.RunCancelled(source="tui")))
    assert snapshot.cancelled
    assert snapshot.outcome is RunOutcome.CANCELLED
    assert snapshot.finished


def test_plan_digest_and_approval_are_recorded() -> None:
    snapshot = fold(
        stream(
            ev.PlanCreated(plan_digest=Digest("p" * 16), task_count=3),
            ev.PlanApproved(plan_digest=Digest("p" * 16)),
        ),
    )
    assert snapshot.plan_digest == Digest("p" * 16)
    assert snapshot.plan_approved


def test_folding_is_idempotent_for_the_same_stream() -> None:
    events = stream(
        ev.SpecAccepted(spec_digest=Digest("s" * 16)),
        ev.MetricsSampled(
            generation_tps=12.5,
            prompt_tps=300.0,
            context_fill=0.62,
            vram_used_mb=3_500,
            ram_used_mb=18_000,
        ),
    )
    assert fold(events) == fold(events)
    assert fold(events).context_fill == 0.62


def test_reasoning_is_an_event_like_any_other() -> None:
    snapshot = fold(stream(ev.ModelReasoning(text="thinking out loud")))
    assert snapshot.seq == 1


def test_a_tasks_conclusion_is_what_a_resume_reads() -> None:
    snapshot = fold(
        stream(
            ev.StepStarted(
                stage="implement",
                role="implementer",
                task_id="t1",
                max_tool_calls=1,
                max_tokens=1,
                max_wall_seconds=1,
            ),
            ev.TaskFinished(task_id="t1", outcome="completed", revisions=0, commits=("cafe",)),
            ev.StepStarted(
                stage="implement",
                role="implementer",
                task_id="t2",
                max_tool_calls=1,
                max_tokens=1,
                max_wall_seconds=1,
            ),
            ev.TaskFinished(
                task_id="t2",
                outcome="handoff",
                revisions=3,
                reason="fix loop limit of 3 reached",
            ),
        ),
    )
    by_id = {task.task_id: task for task in snapshot.tasks}
    assert by_id["t1"].done, "a resume skips this one"
    assert by_id["t1"].commits == ("cafe",)
    assert not by_id["t2"].done
    assert by_id["t2"].failed
    assert "fix loop limit" in by_id["t2"].reason

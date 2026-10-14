# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The event log is append-only, ordered, durable and replayable."""

import json
from pathlib import Path

import pytest

from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.domain import events as ev
from niharness.domain.events import Event, EventBody, RunOutcome, StopReason
from niharness.domain.identity import CallId, Digest, RunId, StepId
from niharness.domain.state import fold

RUN = RunId("abc123")


@pytest.fixture
def sink(tmp_path: Path) -> JsonlEventSink:
    return JsonlEventSink(tmp_path / "runs", RUN)


async def test_sequence_numbers_start_at_one_and_increase(sink: JsonlEventSink) -> None:
    first = await sink.append(
        ev.RunStarted(branch="harness/run-abc123", profile="cpu-only", model="m")
    )
    second = await sink.append(ev.ModelReasoning(text="thinking"))
    assert (first.seq, second.seq) == (1, 2)
    assert first.ts <= second.ts
    assert first.run_id == RUN


async def test_the_step_id_is_stored_on_the_envelope(sink: JsonlEventSink) -> None:
    event = await sink.append(
        ev.StepStarted(
            stage="implement",
            role="implementer",
            max_tool_calls=5,
            max_tokens=100,
            max_wall_seconds=10,
        ),
        step_id=StepId("step-1"),
    )
    assert event.step_id == StepId("step-1")


async def test_every_event_is_one_line_of_json(sink: JsonlEventSink) -> None:
    await sink.append(ev.ModelReasoning(text="a\nb\nc"))
    await sink.append(ev.NotesAppended(section="blocked", text_digest=Digest("n" * 16)))
    lines = sink.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["body"]["type"] == "model.reasoning"


async def test_replay_returns_what_was_appended(sink: JsonlEventSink) -> None:
    bodies: list[EventBody] = [
        ev.RunStarted(branch="b", profile="cpu-only", model="m"),
        ev.ToolCalled(
            call_id=CallId("c1"),
            name="grep",
            arguments={"pattern": "TODO"},
            arguments_digest=Digest("d" * 16),
        ),
        ev.ToolReturned(
            call_id=CallId("c1"),
            ok=True,
            result_digest=Digest("e" * 16),
            truncated=False,
            summary="3 hits",
        ),
        ev.RunFinished(outcome=RunOutcome.COMPLETED),
    ]
    for body in bodies:
        await sink.append(body)
    replayed: list[Event] = [event async for event in sink.replay(RUN)]
    assert [event.type for event in replayed] == [body.type for body in bodies]
    assert [event.seq for event in replayed] == [1, 2, 3, 4]


async def test_a_second_sink_continues_the_sequence(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    first = JsonlEventSink(root, RUN)
    await first.append(ev.ModelReasoning(text="one"))
    second = JsonlEventSink(root, RUN)
    event = await second.append(ev.ModelReasoning(text="two"))
    assert event.seq == 2
    assert len((root / RUN / "events.jsonl").read_text().splitlines()) == 2


async def test_replaying_a_run_that_never_ran_yields_nothing(sink: JsonlEventSink) -> None:
    assert [event async for event in sink.replay(RunId("nothing-here"))] == []


async def test_the_stream_folds_into_a_snapshot(sink: JsonlEventSink) -> None:
    await sink.append(ev.RunStarted(branch="b", profile="cpu-only", model="m"))
    await sink.append(
        ev.VcsCommitted(
            sha="cafe", subject="feat: x", files_changed=1, insertions=2, deletions=0
        ),
    )
    await sink.append(
        ev.StepFinished(
            stop_reason=StopReason.FINAL_ANSWER,
            tool_calls_used=3,
            tokens_used=500,
            wall_seconds=4.0,
        ),
    )
    snapshot = fold([event async for event in sink.replay(RUN)], RUN)
    assert (snapshot.seq, snapshot.commits, snapshot.tokens_used) == (3, ("cafe",), 500)


async def test_snapshots_round_trip_and_the_newest_wins(sink: JsonlEventSink) -> None:
    await sink.append(ev.ModelReasoning(text="x"))
    stream = [event async for event in sink.replay(RUN)]
    first = fold(stream, RUN)
    await sink.write_snapshot(first)
    await sink.append(ev.ModelReasoning(text="y"))
    later = fold([event async for event in sink.replay(RUN)], RUN)
    await sink.write_snapshot(later)
    assert await sink.read_snapshot(RUN) == later


async def test_no_snapshot_is_not_an_error(sink: JsonlEventSink) -> None:
    assert await sink.read_snapshot(RUN) is None


async def test_compaction_does_not_touch_the_log(sink: JsonlEventSink) -> None:
    await sink.append(ev.ModelReasoning(text="kept forever"))
    await sink.append(
        ev.ContextCompacted(before_tokens=9_000, after_tokens=3_000, deterministic_only=False),
    )
    types = [event.type async for event in sink.replay(RUN)]
    assert types == ["model.reasoning", "context.compacted"]

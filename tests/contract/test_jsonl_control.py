# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The control inbox: many writers, one reader, nothing lost."""

from pathlib import Path

import pytest

from niharness.adapters.jsonl_control import JsonlControlChannel
from niharness.domain.artifacts import Answer
from niharness.domain.control import AnswerQuestions, ApprovePlan, CancelRun, Driver
from niharness.domain.identity import Digest, RunId

RUN = RunId("20261022-120000-abc123")


@pytest.fixture
def channel(tmp_path: Path) -> JsonlControlChannel:
    return JsonlControlChannel(tmp_path / "runs", RUN)


def cancel(reason: str = "", source: Driver = Driver.CLI) -> CancelRun:
    return CancelRun(run_id=RUN, reason=reason, source=source)


async def test_a_submitted_command_comes_back_once(channel: JsonlControlChannel) -> None:
    submitted = await channel.submit(cancel("the plan was wrong"))
    drained = await channel.drain()
    assert [message.id for message in drained] == [submitted.id]
    assert isinstance(drained[0].command, CancelRun)
    assert drained[0].command.reason == "the plan was wrong"
    assert await channel.drain() == (), "a drained message is not handed out twice"


async def test_arrival_order_is_preserved(channel: JsonlControlChannel) -> None:
    await channel.submit(cancel("first"))
    await channel.submit(ApprovePlan(plan_digest=Digest("0123456789abcdef")))
    await channel.submit(
        AnswerQuestions(run_id=RUN, answers=(Answer(question_id="q1", free_text="yes"),)),
    )
    kinds = [message.kind for message in await channel.drain()]
    assert kinds == ["cancel_run", "approve_plan", "answer_questions"]


async def test_two_drivers_submitting_both_land(tmp_path: Path) -> None:
    first = JsonlControlChannel(tmp_path / "runs", RUN)
    second = JsonlControlChannel(tmp_path / "runs", RUN)
    await first.submit(cancel("from the tui", Driver.TUI))
    await second.submit(cancel("from an agent", Driver.MCP))
    sources = [message.command.source for message in await first.drain()]
    assert sources == [Driver.TUI, Driver.MCP]


async def test_the_cursor_survives_a_new_reader(tmp_path: Path) -> None:
    writer = JsonlControlChannel(tmp_path / "runs", RUN)
    await writer.submit(cancel("one"))
    assert len(await writer.drain()) == 1
    await writer.submit(cancel("two"))
    reopened = JsonlControlChannel(tmp_path / "runs", RUN)
    drained = await reopened.drain()
    assert len(drained) == 1
    assert isinstance(drained[0].command, CancelRun)
    assert drained[0].command.reason == "two"


async def test_pending_counts_without_consuming(channel: JsonlControlChannel) -> None:
    assert await channel.pending() == 0
    await channel.submit(cancel())
    await channel.submit(cancel("again"))
    assert await channel.pending() == 2
    assert len(await channel.drain()) == 2
    assert await channel.pending() == 0


async def test_an_unreadable_line_does_not_stall_the_run(channel: JsonlControlChannel) -> None:
    await channel.submit(cancel("good"))
    with channel.inbox_path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    await channel.submit(cancel("also good"))
    drained = await channel.drain()
    assert len(drained) == 2, "the broken line is skipped, the others are not"
    assert await channel.pending() == 0


async def test_draining_an_empty_inbox_is_not_an_error(channel: JsonlControlChannel) -> None:
    assert await channel.drain() == ()
    assert await channel.pending() == 0


async def test_the_inbox_is_a_file_a_human_can_read(channel: JsonlControlChannel) -> None:
    await channel.submit(cancel("look at me"))
    lines = channel.inbox_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"cancel_run"' in lines[0]
    assert "look at me" in lines[0]


async def test_a_line_written_without_an_identity_gets_one(
    channel: JsonlControlChannel,
) -> None:
    """The TUI writes these lines with a JSON serialiser and nothing else."""
    channel.inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with channel.inbox_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"ts": 1766400000.5, "command": {"kind": "cancel_run", "source": "tui",'
            f' "run_id": "{RUN}", "reason": "esc"}}}}\n',
        )
    drained = await channel.drain()
    assert len(drained) == 1
    assert drained[0].id, "the inbox assigned an identity"
    assert len(drained[0].id) == 16
    assert isinstance(drained[0].command, CancelRun)
    assert drained[0].command.source is Driver.TUI
    assert drained[0].ts.tzinfo is not None, "an epoch timestamp is read as an instant in UTC"

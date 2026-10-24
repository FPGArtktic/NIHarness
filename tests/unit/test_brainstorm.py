# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The brainstorm session: many steps, one window, one budget."""

import io
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from niharness.application.brainstorm import (
    BrainstormSession,
    SessionNotOpenError,
)
from niharness.application.step_runner import StepRunner
from niharness.domain import events as ev
from niharness.domain.artifacts import AcceptanceCriterion, SpecDraft, SpecOrigin
from niharness.domain.events import Event, EventBody, RunOutcome, StopReason
from niharness.domain.identity import RunId, StepId
from niharness.domain.messages import ChatRequest, ChatResponse, Message, ToolSchema, Usage
from niharness.domain.state import RunSnapshot
from niharness.domain.tools import AcceptedCall, ToolId, ToolResult


def draft(title: str = "Faster gate") -> SpecDraft:
    return SpecDraft(
        title=title,
        goal="The quick gate finishes in under a minute on this host.",
        acceptance_criteria=(
            AcceptanceCriterion(
                id="c1",
                statement="the quick tier finishes in under 60s",
                verification="time uv run pytest -q, under 60 seconds",
            ),
        ),
    )


def says(text: str) -> ChatResponse:
    return ChatResponse(
        content=text,
        finish_reason="stop",
        usage=Usage(prompt_tokens=100, completion_tokens=40),
    )


def writes(spec: SpecDraft) -> ChatResponse:
    return ChatResponse(
        content=spec.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=200, completion_tokens=90),
    )


class FakeBackend:
    def __init__(self, *responses: ChatResponse) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses.pop(0) if self._responses else says("nothing more to add")

    async def count_text(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def count_messages(self, messages: Sequence[Message]) -> int:
        return sum(max(1, len(message.content) // 4) + 3 for message in messages)


class FakeTools:
    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(name=tool, description=tool, parameters={"type": "object"})
            for tool in sorted(allowlist)
        )

    async def call(self, accepted: AcceptedCall) -> ToolResult:  # pragma: no cover
        raise AssertionError("a brainstorm turn in these tests calls no tool")


class FakeSink:
    def __init__(self) -> None:
        self.bodies: list[EventBody] = []

    async def append(self, body: EventBody, *, step_id: StepId | None = None) -> Event:
        self.bodies.append(body)
        return Event(
            seq=len(self.bodies),
            ts=datetime.now(UTC),
            run_id=RunId("r"),
            step_id=step_id,
            body=body,
        )

    async def replay(self, run_id: RunId) -> AsyncIterator[Event]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]

    async def write_snapshot(self, snapshot: RunSnapshot) -> None:  # pragma: no cover
        return None

    async def read_snapshot(self, run_id: RunId) -> RunSnapshot | None:  # pragma: no cover
        return None


def session(backend: FakeBackend, sink: FakeSink) -> BrainstormSession:
    return BrainstormSession(
        runner=StepRunner(backend=backend, tools=FakeTools(), events=sink),  # type: ignore[arg-type]
        events=sink,  # type: ignore[arg-type]
        tokenizer=backend,  # type: ignore[arg-type]
        model="Qwen3-30B-A3B-Thinking",
    )


async def opened(backend: FakeBackend, sink: FakeSink) -> BrainstormSession:
    live = session(backend, sink)
    await live.open(
        RunId("r"),
        topic="make the gate faster",
        branch="harness/run-r",
        profile="p",
        tools="local",
    )
    return live


async def test_a_turn_is_a_step_and_what_the_human_said_is_an_event() -> None:
    """§4: everything is an event, and the human's side is the only record of it.

    The model's side is a `model.response` with a digest, because §15 keeps
    unvalidated model output out of the log. The human's words have no such
    problem and a session nobody can read afterwards did not happen.
    """
    backend, sink = FakeBackend(says("I read src/gate.py; the tests dominate.")), FakeSink()
    live = await opened(backend, sink)
    turn = await live.say("where does the time go?")

    assert turn.number == 1
    assert turn.said == "I read src/gate.py; the tests dominate."
    assert turn.stop_reason is StopReason.FINAL_ANSWER
    said = next(body for body in sink.bodies if isinstance(body, ev.BrainstormSaid))
    assert said.turn == 1
    assert said.text == "where does the time go?"


async def test_the_window_is_one_conversation_across_turns() -> None:
    """A conversation is many model calls over one window.

    §4 says history never crosses a *stage* boundary, not that a stage may
    call a model once - and a brainstormer that forgot the last question
    would be a worse interlocutor than a text file.
    """
    backend, sink = FakeBackend(says("first"), says("second")), FakeSink()
    live = await opened(backend, sink)
    await live.say("what is slow?")
    await live.say("and why?")

    assert len(backend.requests) == 2
    second = backend.requests[-1]
    asked = [message.content for message in second.messages]
    assert "what is slow?" in asked, "the first question is still in the window"
    assert "first" in asked, "and so is the answer to it"
    assert "and why?" in asked


async def test_the_budget_belongs_to_the_stage_not_to_a_turn() -> None:
    """Otherwise a conversation runs forever by being a conversation."""
    backend, sink = FakeBackend(says("a"), says("b")), FakeSink()
    live = await opened(backend, sink)
    assert live.spent == (0, 0)
    await live.say("one")
    after_one = live.spent[1]
    assert after_one > 0
    await live.say("two")
    assert live.spent[1] > after_one, "the second turn spends what the first did not"
    assert live.exhausted is False, "the stage's budget is larger than two turns"


async def test_the_closing_turn_has_no_tools_and_the_harness_binds_the_origin() -> None:
    """`Spec.origin` is a field a model could only transcribe (ADR 0026)."""
    backend, sink = FakeBackend(says("here is what I would write"), writes(draft())), FakeSink()
    live = await opened(backend, sink)
    await live.say("summarise")
    spec = await live.spec(SpecOrigin.BRAINSTORM)

    assert spec is not None
    assert spec.origin is SpecOrigin.BRAINSTORM, "bound, not asked for"
    assert spec.title == "Faster gate"
    closing = backend.requests[-1]
    assert closing.tools == (), "no tool is offered on the closing turn"
    assert closing.response_format is not None, "and the artifact's schema is attached"
    assert closing.response_format.name == "SpecDraft"


async def test_a_closing_turn_that_writes_no_valid_draft_produces_no_spec() -> None:
    """Nothing is written rather than something invented."""
    backend, sink = FakeBackend(says("I would rather not")), FakeSink()
    live = await opened(backend, sink)
    assert await live.spec(SpecOrigin.BRAINSTORM) is None


async def test_a_session_used_before_it_is_opened_says_so() -> None:
    with pytest.raises(SessionNotOpenError, match="not opened"):
        await session(FakeBackend(), FakeSink()).say("hello")


async def test_closing_records_the_run_and_what_it_cost() -> None:
    backend, sink = FakeBackend(says("ok")), FakeSink()
    live = await opened(backend, sink)
    await live.say("one")
    result = await live.close(RunOutcome.CANCELLED, "the human ended the session")

    assert result.outcome is RunOutcome.CANCELLED
    assert result.turns == 1
    assert result.tokens > 0
    assert result.spec is None
    finished = next(body for body in sink.bodies if isinstance(body, ev.RunFinished))
    assert finished.reason == "the human ended the session"


def test_the_reader_stops_at_the_end_of_the_input() -> None:
    """A session driven from a file or a pipe ends when the input does."""
    from niharness.cli.brainstorm import _asked

    stream = io.StringIO("one\n\n")
    assert _asked(stream, quiet=True) == "one"
    assert _asked(stream, quiet=True) == ""
    assert _asked(stream, quiet=True) is None

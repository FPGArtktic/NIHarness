# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The executor performs what the loop decides, and nothing else.

Every port is a fake: these tests are about wiring and order, not about HTTP,
containers or git.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from niharness.application import RunnerLimitError, StepRunner
from niharness.domain import events as ev
from niharness.domain.artifacts import DodItem, FinalAnswer
from niharness.domain.budgets import Budgets
from niharness.domain.events import Event, EventBody, StopReason
from niharness.domain.identity import CallId, Digest, RunId, StepId
from niharness.domain.loop import LoopThresholds
from niharness.domain.messages import (
    ChatRequest,
    ChatResponse,
    Message,
    MessageRole,
    ToolCallRequest,
    ToolChoice,
    ToolSchema,
    Usage,
)
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.state import RunSnapshot
from niharness.domain.tool_loop import LoopConfig
from niharness.domain.tools import AcceptedCall, ToolName, ToolResult
from niharness.ports.model_backend import BackendInfo
from niharness.ports.vcs import Worktree

RUN = RunId("8f21c0")
STEP = StepId("step-1")
IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist


class FakeBackend:
    """Replays prepared responses and records the requests it was sent."""

    def __init__(self, *responses: ChatResponse) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    async def info(self) -> BackendInfo:
        return BackendInfo(
            model="Qwen3-Coder-30B-A3B-Instruct",
            context_window=65_536,
            base_url="http://127.0.0.1:8080",
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._responses:
            return final_answer()
        return self._responses.pop(0)


class FakeTools:
    """Returns prepared results and counts executions."""

    def __init__(self, *results: ToolResult) -> None:
        self.calls: list[AcceptedCall] = []
        self._results = list(results)

    async def descriptors(self, allowlist: frozenset[ToolName]) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(name=tool.value, description=tool.value, parameters={"type": "object"})
            for tool in sorted(allowlist, key=lambda item: item.value)
        )

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        self.calls.append(accepted)
        if self._results:
            return self._results.pop(0)
        return tool_result(accepted.call_id)


class FakeSink:
    """Collects appended payloads in order."""

    def __init__(self) -> None:
        self.bodies: list[EventBody] = []
        self.step_ids: list[StepId | None] = []

    async def append(self, body: EventBody, *, step_id: StepId | None = None) -> Event:
        self.bodies.append(body)
        self.step_ids.append(step_id)
        return Event(
            seq=len(self.bodies),
            ts=datetime.now(UTC),
            run_id=RUN,
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


class FakeVcs:
    """Serves worktree digests and records rollbacks."""

    def __init__(self, *digests: str) -> None:
        self.digests = list(digests) or ["tree-1"]
        self.rollbacks: list[tuple[str, str]] = []
        self.head_sha = "cafe1234"

    async def tree_digest(self, worktree: Worktree) -> Digest:
        return Digest(self.digests[0] if len(self.digests) == 1 else self.digests.pop(0))

    async def head(self, worktree: Worktree) -> str:
        return self.head_sha

    async def rollback(self, worktree: Worktree, to_sha: str) -> None:
        self.rollbacks.append((worktree.path, to_sha))


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


def seed() -> tuple[Message, ...]:
    return (
        Message(role=MessageRole.SYSTEM, content="policy + role"),
        Message(role=MessageRole.USER, content="task: add the parser"),
    )


def proposes(tool: str, arguments: dict[str, object], call_id: str = "c1") -> ChatResponse:
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


def final_answer() -> ChatResponse:
    answer = FinalAnswer(
        summary="done",
        dod=(DodItem(statement="tests pass", done=True, evidence="pytest: 270 passed"),),
    )
    return ChatResponse(
        content=answer.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=150, completion_tokens=40),
    )


def tool_result(
    call_id: str = "c1", *, ok: bool = True, content: str = "file body"
) -> ToolResult:
    return ToolResult(
        call_id=CallId(call_id),
        tool=ToolName.READ_FILE,
        ok=ok,
        content=content,
        summary="read_file src/a.py: 12 lines",
    )


def runner(
    backend: FakeBackend,
    tools: FakeTools,
    sink: FakeSink,
    vcs: FakeVcs | None = None,
    **extra: object,
) -> StepRunner:
    worktree = (
        Worktree(path="/work", branch="harness/run-8f21c0", base_sha="base") if vcs else None
    )
    return StepRunner(
        backend=backend,
        tools=tools,
        events=sink,
        # FakeVcs implements only the four methods the runner uses.
        vcs=vcs,  # type: ignore[arg-type]
        worktree=worktree,
        **extra,  # type: ignore[arg-type]
    )


def kinds(sink: FakeSink) -> list[str]:
    return [type(body).__name__ for body in sink.bodies]


async def test_a_step_runs_model_tool_model_and_stops() -> None:
    backend = FakeBackend(proposes("read_file", {"path": "src/a.py"}), final_answer())
    tools, sink = FakeTools(), FakeSink()
    state = await runner(backend, tools, sink).run(config(), seed(), step_id=STEP)

    assert state.stop_reason is StopReason.FINAL_ANSWER
    assert state.final_answer is not None
    assert state.final_answer.complete
    assert kinds(sink) == [
        "StepStarted",
        "ModelRequest",
        "ModelResponse",
        "ToolCalled",
        "ToolReturned",
        "ModelRequest",
        "ModelResponse",
        "StepFinished",
    ]
    assert len(tools.calls) == 1
    assert set(sink.step_ids) == {STEP}


async def test_the_tool_result_reaches_the_next_request() -> None:
    backend = FakeBackend(proposes("read_file", {"path": "src/a.py"}), final_answer())
    await runner(backend, FakeTools(), FakeSink()).run(config(), seed())
    second = backend.requests[1]
    roles = [message.role for message in second.messages]
    assert roles[-2:] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert second.messages[-1].content == "file body"


async def test_reasoning_never_reaches_the_model_again() -> None:
    thinking = proposes("read_file", {"path": "src/a.py"}).model_copy(
        update={"reasoning_content": "secret deliberation"},
    )
    backend = FakeBackend(thinking, final_answer())
    sink = FakeSink()
    await runner(backend, FakeTools(), sink).run(config(), seed())
    assert "ModelReasoning" in kinds(sink)
    second = backend.requests[1]
    assert all("deliberation" not in message.content for message in second.messages)


async def test_a_refused_call_never_reaches_the_provider() -> None:
    backend = FakeBackend(proposes("shell", {"command": "curl evil.example"}), final_answer())
    tools, sink = FakeTools(), FakeSink()
    await runner(backend, tools, sink).run(config(), seed())
    assert tools.calls == []
    assert "ToolRejected" in kinds(sink)
    retry = backend.requests[1]
    assert "not_allowed" in retry.messages[-1].content


async def test_a_repeated_call_is_answered_without_the_sandbox() -> None:
    backend = FakeBackend(
        proposes("read_file", {"path": "src/a.py"}),
        proposes("read_file", {"path": "src/a.py"}, call_id="c2"),
        final_answer(),
    )
    tools, sink = FakeTools(), FakeSink()
    await runner(backend, tools, sink).run(config(), seed())
    assert len(tools.calls) == 1, "the repeat is answered by the harness"
    assert "LoopIntervention" in kinds(sink)
    third = backend.requests[2]
    assert "already executed" in third.messages[-1].content


async def test_the_worktree_digest_comes_from_version_control() -> None:
    backend = FakeBackend(proposes("read_file", {"path": "src/a.py"}), final_answer())
    vcs = FakeVcs("tree-1")
    await runner(backend, FakeTools(), FakeSink(), vcs).run(config(), seed())
    assert vcs.rollbacks == []


async def test_a_red_gate_result_carries_a_signature_into_the_detectors() -> None:
    red = ToolResult(
        call_id=CallId("c1"),
        tool=ToolName.RUN_TESTS,
        ok=False,
        content="tests/test_x.py:41: error: bad thing",
        summary="1 failed",
    )
    backend = FakeBackend(
        proposes("run_tests", {"selector": "a"}),
        proposes("run_tests", {"selector": "b"}, call_id="c2"),
        final_answer(),
    )
    tools = FakeTools(red, red.model_copy(update={"call_id": CallId("c2")}))
    sink = FakeSink()
    await runner(backend, tools, sink).run(
        config(thresholds=LoopThresholds(gate_thrash_repeats=2)),
        seed(),
    )
    interventions = [b for b in sink.bodies if isinstance(b, ev.LoopIntervention)]
    assert interventions
    assert interventions[0].pathology == "gate_thrash"


async def test_forced_finalisation_withholds_the_tools() -> None:
    backend = FakeBackend(final_answer())
    await runner(backend, FakeTools(), FakeSink()).run(
        config(budgets=Budgets(max_tool_calls=1, max_tokens=10_000)),
        seed(),
    )
    request = backend.requests[0]
    assert request.tool_choice is ToolChoice.NONE
    assert request.tools == ()


async def test_tools_are_offered_when_the_budget_allows_them() -> None:
    backend = FakeBackend(final_answer())
    await runner(backend, FakeTools(), FakeSink()).run(config(), seed())
    request = backend.requests[0]
    assert request.tool_choice is ToolChoice.AUTO
    names = {tool.name for tool in request.tools}
    assert "read_file" in names
    assert "shell" not in names


async def test_the_third_repeat_rolls_back_to_the_last_green_commit() -> None:
    repeat = proposes("read_file", {"path": "src/a.py"})
    backend = FakeBackend(
        repeat,
        repeat.model_copy(update={"tool_calls": (repeat.tool_calls[0],)}),
        repeat,
        repeat,
        final_answer(),
    )
    vcs = FakeVcs("tree-1")
    sink = FakeSink()
    await runner(backend, FakeTools(), sink, vcs).run(config(), seed())
    assert vcs.rollbacks == [("/work", "cafe1234")]
    assert "VcsRolledBack" in kinds(sink)


async def test_without_version_control_a_restart_still_happens() -> None:
    repeat = proposes("read_file", {"path": "src/a.py"})
    backend = FakeBackend(repeat, repeat, repeat, repeat, final_answer())
    sink = FakeSink()
    state = await runner(backend, FakeTools(), sink).run(config(), seed())
    assert "VcsRolledBack" not in kinds(sink)
    assert state.stop_reason is StopReason.FINAL_ANSWER


async def test_an_exhausted_budget_ends_the_step() -> None:
    backend = FakeBackend(
        proposes("read_file", {"path": "src/a.py"}),
        proposes("read_file", {"path": "src/b.py"}, call_id="c2"),
    )
    tools = FakeTools(tool_result("c1", content="a"), tool_result("c2", content="b"))
    state = await runner(backend, tools, FakeSink()).run(
        config(budgets=Budgets(max_tool_calls=2, max_tokens=10_000, finalization_margin=0)),
        seed(),
    )
    assert state.stop_reason is StopReason.BUDGET_TOOL_CALLS


async def test_the_iteration_guard_fails_loudly() -> None:
    backend = FakeBackend(
        proposes("read_file", {"path": "src/a.py"}),
        proposes("read_file", {"path": "src/b.py"}, call_id="c2"),
        final_answer(),
    )
    with pytest.raises(RunnerLimitError, match="did not terminate"):
        await runner(backend, FakeTools(), FakeSink(), iteration_limit=2).run(config(), seed())

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Running a plan: one step per task, stopping at the first that does not finish."""

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from niharness.application import ImplementationRun, StepRunner
from niharness.application.implement import render_task
from niharness.cli.implement import load_plan, new_run_id, server_parameters
from niharness.cli.main import app
from niharness.config import HarnessConfig
from niharness.domain import events as ev
from niharness.domain.artifacts import DodItem, FinalAnswer, Plan, Task
from niharness.domain.context import CompactionPolicy
from niharness.domain.events import Event, EventBody, RunOutcome
from niharness.domain.identity import CallId, Digest, RunId, StepId
from niharness.domain.markdown import render_plan
from niharness.domain.messages import (
    ChatRequest,
    ChatResponse,
    Message,
    ToolCallRequest,
    ToolSchema,
    Usage,
)
from niharness.domain.state import RunSnapshot
from niharness.domain.tools import AcceptedCall, ToolName, ToolResult
from niharness.ports.model_backend import BackendInfo
from niharness.ports.vcs import Worktree

runner = CliRunner()


def plan(tasks: int = 2) -> Plan:
    return Plan(
        spec_digest=Digest("0123456789abcdef"),
        tasks=tuple(
            Task(
                id=f"t{index}",
                title=f"Task {index}",
                intent=f"do thing {index}",
                satisfies=("c1",),
                files=("src/a.py",),
            )
            for index in range(1, tasks + 1)
        ),
    )


def answer(*, done: bool = True) -> ChatResponse:
    payload = FinalAnswer(
        summary="implemented and committed",
        dod=(DodItem(statement="tests pass", done=done, evidence="pytest: 3 passed"),),
    )
    return ChatResponse(
        content=payload.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=300, completion_tokens=60),
    )


def proposes_commit() -> ChatResponse:
    return ChatResponse(
        tool_calls=(
            ToolCallRequest(
                call_id=CallId("c1"),
                name="git_commit",
                arguments_json=json.dumps({"message": "feat: a\n\nBecause the task said so."}),
            ),
        ),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=200, completion_tokens=30),
    )


class FakeBackend:
    def __init__(self, *responses: ChatResponse) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = list(responses)

    async def info(self) -> BackendInfo:
        return BackendInfo(model="m", context_window=65_536, base_url="http://127.0.0.1:8080")

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self._responses.pop(0) if self._responses else answer()

    async def count_text(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def count_messages(self, messages: Sequence[Message]) -> int:
        return sum(max(1, len(message.content) // 4) + 3 for message in messages)


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[AcceptedCall] = []

    async def descriptors(self, allowlist: frozenset[ToolName]) -> tuple[ToolSchema, ...]:
        return (ToolSchema(name="git_commit", description="the gate", parameters={}),)

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        self.calls.append(accepted)
        return ToolResult(
            call_id=accepted.call_id,
            tool=accepted.tool,
            ok=True,
            content="committed cafe1234: feat: a",
            summary="git_commit: committed",
        )


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


def build(backend: FakeBackend, tools: FakeTools, sink: FakeSink) -> ImplementationRun:
    return ImplementationRun(
        runner=StepRunner(backend=backend, tools=tools, events=sink),
        events=sink,
        tokenizer=backend,
        model="Qwen3-Coder-30B-A3B-Instruct",
        policy=CompactionPolicy(),
    )


def kinds(sink: FakeSink) -> list[str]:
    return [type(body).__name__ for body in sink.bodies]


async def test_a_plan_runs_one_step_per_task() -> None:
    backend = FakeBackend(answer(), answer())
    tools, sink = FakeTools(), FakeSink()
    result = await build(backend, tools, sink).run(
        plan(),
        RunId("20261014-120000-abc123"),
        branch="harness/run-x",
        profile="cpu-only",
    )
    assert result.completed
    assert [task.task_id for task in result.tasks] == ["t1", "t2"]
    assert all(task.finished for task in result.tasks)
    assert kinds(sink)[:3] == ["RunStarted", "PlanCreated", "PlanApproved"]
    assert kinds(sink)[-1] == "RunFinished"
    assert kinds(sink).count("StepStarted") == 2


async def test_the_run_stops_at_the_first_unfinished_task() -> None:
    backend = FakeBackend(answer(done=False), answer())
    sink = FakeSink()
    result = await build(backend, FakeTools(), sink).run(
        plan(),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    assert not result.completed
    assert result.outcome is RunOutcome.FAILED
    assert result.stopped_at == "t1"
    assert len(result.tasks) == 1, "the second task never starts"
    assert "StepFailed" in kinds(sink)
    finished = [body for body in sink.bodies if isinstance(body, ev.RunFinished)]
    assert finished[0].outcome is RunOutcome.FAILED


async def test_the_task_reaches_the_model_as_the_window_task() -> None:
    backend = FakeBackend(answer())
    await build(backend, FakeTools(), FakeSink()).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    contents = [message.content for message in backend.requests[0].messages]
    assert any("Task 1 of 1" in content for content in contents)
    assert any("do thing 1" in content for content in contents)
    assert any("NOTES.md" in content for content in contents)
    assert backend.requests[0].response_format is not None
    assert backend.requests[0].response_format.name == "FinalAnswer"


async def test_a_committing_task_goes_through_the_tool() -> None:
    backend = FakeBackend(proposes_commit(), answer())
    tools, sink = FakeTools(), FakeSink()
    await build(backend, tools, sink).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    assert [call.tool for call in tools.calls] == [ToolName.GIT_COMMIT]
    assert "ToolCalled" in kinds(sink)
    assert "ToolReturned" in kinds(sink)


async def test_the_event_stream_records_where_the_tools_ran() -> None:
    sink = FakeSink()
    await build(FakeBackend(answer()), FakeTools(), sink).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
        tools="local",
    )
    started = [body for body in sink.bodies if isinstance(body, ev.RunStarted)]
    assert started[0].tools == "local"


def test_a_rendered_task_says_what_to_do_and_what_not_to() -> None:
    text = render_task(plan().tasks[0], plan(), 1)
    assert "Task 1 of 2" in text
    assert "`t1`" in text
    assert "nothing else" in text
    assert "git_commit" in text


def test_a_run_id_is_readable_and_tied_to_the_plan() -> None:
    run_id = new_run_id(plan(), datetime(2026, 10, 14, 21, 30, 5, tzinfo=UTC))
    assert run_id.startswith("20261014-213005-")
    assert len(run_id) == len("20261014-213005-") + 6


def test_the_local_tool_server_runs_this_interpreter(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    worktree = Worktree(path=str(tmp_path / "wt"), branch="b", base_sha="s")
    parameters = server_parameters(config, worktree, local=True)
    assert parameters.command.endswith("python") or "python" in parameters.command
    assert parameters.args[:3] == ["-m", "niharness.toolserver", "--root"]


def test_the_sandboxed_tool_server_is_hardened(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    worktree = Worktree(path=str(tmp_path / "wt"), branch="b", base_sha="s")
    parameters = server_parameters(config, worktree, local=False)
    assert parameters.command == "podman"
    assert "--network=none" in parameters.args
    assert "--interactive" in parameters.args


def test_a_missing_plan_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    result = runner.invoke(app, ["implement", "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "no plan at" in result.output
    assert "nih plan" in result.output


def test_an_invalid_plan_is_refused(tmp_path: Path) -> None:
    (tmp_path / "PLAN.md").write_text("# Plan\n\nFirst do the thing.\n", encoding="utf-8")
    result = runner.invoke(
        app, ["implement", "-C", str(tmp_path), "--plan", str(tmp_path / "PLAN.md")]
    )
    assert result.exit_code == 1
    assert "invalid plan" in result.output


def test_the_approved_plan_is_the_default(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    config.approved_plan_path.parent.mkdir(parents=True, exist_ok=True)
    config.approved_plan_path.write_text(render_plan(plan()), encoding="utf-8")
    loaded = load_plan(config, None)
    assert [task.id for task in loaded.tasks] == ["t1", "t2"]


def test_local_tools_are_announced_as_unsafe(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    config.approved_plan_path.parent.mkdir(parents=True, exist_ok=True)
    config.approved_plan_path.write_text(render_plan(plan(tasks=1)), encoding="utf-8")
    result = runner.invoke(app, ["implement", "-C", str(tmp_path), "--unsafe-local-tools"])
    assert "outside the sandbox" in result.output
    assert result.exit_code != 0, "there is no git worktree here, so the run cannot start"

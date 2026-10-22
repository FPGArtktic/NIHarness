# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The management surface, driven the way an agent would drive it.

A real MCP server and a real client session over the SDK's memory streams, so
what is asserted is what a supervisor would actually receive - including the
tools that are deliberately absent.
"""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_client_server_memory_streams

from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.config import HarnessConfig
from niharness.domain import events as ev
from niharness.domain.artifacts import Plan, Task
from niharness.domain.identity import Digest, RunId
from niharness.domain.markdown import render_plan
from niharness.management.server import build_server

RUN = RunId("20261022-100000-abcdef")


def plan(tasks: int = 2) -> Plan:
    return Plan(
        spec_digest=Digest("0123456789abcdef"),
        tasks=tuple(
            Task(id=f"t{index}", title=f"Task {index}", intent=f"do thing {index}")
            for index in range(1, tasks + 1)
        ),
    )


async def seed_run(config: HarnessConfig, run_id: RunId = RUN) -> None:
    """Write the event log of a run that did one task and failed the second."""
    sink = JsonlEventSink(config.runs_dir, run_id)
    await sink.append(ev.RunStarted(branch="harness/run-x", profile="cpu-only", model="qwen"))
    await sink.append(ev.PlanCreated(plan_digest=plan().digest(), task_count=2, imported=True))
    for index, task in enumerate(("t1", "t2"), start=1):
        await sink.append(
            ev.StepStarted(
                stage="implement",
                role="implementer",
                task_id=task,
                max_tool_calls=40,
                max_tokens=1_000,
                max_wall_seconds=60,
            ),
        )
        if index == 1:
            await sink.append(
                ev.VcsCommitted(
                    sha="cafe1234cafe",
                    subject="feat: a",
                    files_changed=1,
                    insertions=3,
                    deletions=0,
                ),
            )
        else:
            await sink.append(ev.StepFailed(reason="task t2: budget_tool_calls"))


@asynccontextmanager
async def connected(config: HarnessConfig) -> AsyncIterator[ClientSession]:
    """Run the management server in this process and yield a client for it."""
    server = build_server(config)
    inner = server._lowlevel_server  # noqa: SLF001 - the SDK's own in-process pattern
    async with AsyncExitStack() as stack:
        client, server_side = await stack.enter_async_context(
            create_client_server_memory_streams(),
        )
        read, write = client
        server_read, server_write = server_side
        group = await stack.enter_async_context(anyio.create_task_group())

        async def serve() -> None:
            await inner.run(server_read, server_write, inner.create_initialization_options())

        group.start_soon(serve)
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        yield session
        group.cancel_scope.cancel()


def text_of(result: types.CallToolResult) -> str:
    """Return the digest a tool answered with."""
    structured = result.structured_content
    assert isinstance(structured, dict)
    return str(structured["text"])


def ok_of(result: types.CallToolResult) -> bool:
    structured = result.structured_content
    assert isinstance(structured, dict)
    return bool(structured["ok"])


@pytest.fixture
def project(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig.load(tmp_path)


async def test_the_surface_has_no_way_to_touch_the_code(project: HarnessConfig) -> None:
    async with connected(project) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert "read_file" not in names
    assert "apply_patch" not in names
    assert "git_commit" not in names
    assert "run_lint" not in names, "the gate is not something a supervisor runs"
    assert {"runs_list", "run_status", "implement_start", "run_cancel"} <= names


async def test_listing_runs_of_an_empty_project(project: HarnessConfig) -> None:
    async with connected(project) as session:
        result = await session.call_tool("runs_list", {})
    assert ok_of(result)
    assert "no runs" in text_of(result)


async def test_status_and_tasks_come_from_the_event_log(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        status = text_of(await session.call_tool("run_status", {"run_id": RUN}))
        tasks = text_of(await session.call_tool("run_tasks", {"run_id": RUN}))
    assert f"run {RUN}" in status
    assert "commits: 1" in status
    assert "[ok  ] t1" in tasks
    assert "[FAIL] t2" in tasks
    assert "budget_tool_calls" in tasks


async def test_events_are_a_digest_with_a_cursor(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        first = await session.call_tool("run_events", {"run_id": RUN, "limit": 3})
        structured = first.structured_content
        assert isinstance(structured, dict)
        cursor = int(structured["cursor"])
        again = await session.call_tool("run_events", {"run_id": RUN, "since": cursor})
    body = text_of(first)
    assert "event(s) since 0" in body
    assert "step.started" in body
    assert len(body) < 2_000, "a digest, not a log"
    assert "nothing new" in text_of(again)


async def test_events_can_be_filtered_to_what_matters(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        result = await session.call_tool(
            "run_events",
            {"run_id": RUN, "kinds": "vcs.committed,step.failed"},
        )
    body = text_of(result)
    assert "vcs.committed" in body
    assert "step.failed" in body
    assert "step.started" not in body


async def test_an_unknown_run_is_refused_not_invented(project: HarnessConfig) -> None:
    async with connected(project) as session:
        result = await session.call_tool("run_status", {"run_id": "nope"})
    assert not ok_of(result)
    assert "no such thing" in text_of(result)


async def test_a_plan_is_submitted_approved_and_then_startable(
    project: HarnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[list[str]] = []

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            started.append(argv)

    monkeypatch.setattr("niharness.management.runs.subprocess.Popen", FakeProcess)

    async with connected(project) as session:
        submitted = await session.call_tool("plan_submit", {"markdown": render_plan(plan())})
        structured = submitted.structured_content
        assert isinstance(structured, dict)
        digest = str(structured["plan_digest"])
        assert "2 task(s)" in text_of(submitted)

        approved = await session.call_tool("plan_approve", {"plan_digest": digest})
        assert ok_of(approved)

        run = await session.call_tool("implement_start", {})
        run_structured = run.structured_content
        assert isinstance(run_structured, dict)
        run_id = str(run_structured["run_id"])

    assert ok_of(run)
    assert "in the sandbox" in text_of(run)
    assert started, "a detached child was started"
    assert "--run-id" in started[0]
    assert started[0][started[0].index("--run-id") + 1] == run_id
    assert project.approved_plan_path.is_file()


async def test_starting_without_an_approved_plan_says_so(project: HarnessConfig) -> None:
    async with connected(project) as session:
        result = await session.call_tool("implement_start", {})
    assert not ok_of(result)
    assert "plan_approve" in text_of(result)


async def test_a_plan_that_does_not_validate_is_refused(project: HarnessConfig) -> None:
    async with connected(project) as session:
        result = await session.call_tool("plan_submit", {"markdown": "# Plan\n\nprose\n"})
    assert not ok_of(result)
    assert "does not validate" in text_of(result)


async def test_cancelling_writes_to_the_inbox(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        result = await session.call_tool("run_cancel", {"run_id": RUN, "reason": "enough"})
    assert ok_of(result)
    inbox = (project.runs_dir / RUN / "control.jsonl").read_text(encoding="utf-8")
    assert '"cancel_run"' in inbox
    assert '"mcp"' in inbox, "the run's history says who steered it"
    assert "enough" in inbox


async def test_answers_reach_the_inbox(project: HarnessConfig) -> None:
    await seed_run(project)
    answers = json.dumps([{"question_id": "q1", "free_text": "use the second approach"}])
    async with connected(project) as session:
        result = await session.call_tool(
            "questions_answer",
            {"run_id": RUN, "answers_json": answers},
        )
    assert ok_of(result)
    inbox = (project.runs_dir / RUN / "control.jsonl").read_text(encoding="utf-8")
    assert '"answer_questions"' in inbox
    assert "second approach" in inbox


async def test_malformed_answers_are_refused(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        result = await session.call_tool(
            "questions_answer",
            {"run_id": RUN, "answers_json": "{}"},
        )
    assert not ok_of(result)
    assert "non-empty JSON array" in text_of(result)


async def test_the_configuration_is_reported_without_the_whole_file(
    project: HarnessConfig,
) -> None:
    async with connected(project) as session:
        result = await session.call_tool("config_show", {})
    body = text_of(result)
    assert "profile:" in body
    assert "main model:" in body
    assert "network none" in body
    assert len(body) < 1_000


async def test_a_diff_is_refused_when_the_worktree_is_gone(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        result = await session.call_tool("run_diff", {"run_id": RUN})
    assert "worktree" in text_of(result)


async def test_a_run_that_is_still_live_is_not_resumed(
    project: HarnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_run(project)
    monkeypatch.setattr("niharness.management.runs.RunRegistry.is_live", lambda self, run: True)
    async with connected(project) as session:
        result = await session.call_tool("run_resume", {"run_id": RUN})
    assert not ok_of(result)
    assert "still running" in text_of(result)


async def test_resuming_needs_the_plan_that_was_running(project: HarnessConfig) -> None:
    await seed_run(project)
    async with connected(project) as session:
        result = await session.call_tool("run_resume", {"run_id": RUN})
    assert not ok_of(result)
    assert "not stored" in text_of(result), "the plan was never submitted here"


async def test_a_stopped_run_is_resumed_where_it_stopped(
    project: HarnessConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[list[str]] = []

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            started.append(argv)

    monkeypatch.setattr("niharness.management.runs.subprocess.Popen", FakeProcess)
    await seed_run(project)
    sink = JsonlEventSink(project.runs_dir, RUN)
    await sink.append(
        ev.TaskFinished(
            task_id="t1", outcome="completed", revisions=0, commits=("cafe1234cafe",)
        ),
    )
    await sink.append(ev.RunCancelled(source="mcp"))

    async with connected(project) as session:
        await session.call_tool("plan_submit", {"markdown": render_plan(plan())})
        result = await session.call_tool("run_resume", {"run_id": RUN})

    assert ok_of(result), text_of(result)
    assert "skipping 1 finished task" in text_of(result)
    assert started, "a detached child continued the run"
    assert started[0][3] == "resume"
    assert started[0][4] == RUN

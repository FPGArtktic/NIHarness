# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Running a plan: one step per task, stopping at the first that does not finish."""

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from niharness.application import ImplementationRun, StepRunner
from niharness.application.implement import render_task
from niharness.cli.implement import load_plan
from niharness.cli.main import app
from niharness.cli.wiring import server_parameters
from niharness.config import HarnessConfig
from niharness.domain import events as ev
from niharness.domain.artifacts import (
    Decision,
    DodItem,
    FinalAnswer,
    Plan,
    ReviewNote,
    Severity,
    Task,
    Verdict,
)
from niharness.domain.context import CompactionPolicy
from niharness.domain.control import (
    ApprovePlan,
    CancelRun,
    ControlCommand,
    ControlMessage,
    Driver,
    message_id,
)
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
from niharness.domain.pipeline import Stage
from niharness.domain.state import RunSnapshot
from niharness.domain.tools import (
    AcceptedCall,
    ExternalTool,
    ToolCatalog,
    ToolId,
    ToolName,
    ToolResult,
)
from niharness.management.runs import RunRegistry, new_run_id
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


def verdict(*, ack: bool = True) -> ChatResponse:
    payload = Verdict(
        decision=Decision.ACK if ack else Decision.NAK,
        summary="looks right" if ack else "the error is discarded",
        build_passed=True,
        tests_passed=True,
        notes=()
        if ack
        else (
            ReviewNote(
                file="src/a.py",
                line=12,
                severity=Severity.BLOCKER,
                message="the result of the inner call is dropped",
            ),
        ),
    )
    return ChatResponse(
        content=payload.model_dump_json(),
        finish_reason="stop",
        usage=Usage(prompt_tokens=400, completion_tokens=80),
    )


def gathers(tool: str) -> ChatResponse:
    """The reviewer gathering the evidence its verdict will claim.

    Not optional any more: a verdict asserting that the build and the tests
    passed is sent back once when nothing in its own step ran them. §4 has
    always said the reviewer re-runs both itself; now the harness checks.
    """
    return ChatResponse(
        tool_calls=(
            ToolCallRequest(
                call_id=CallId(f"e-{tool}"),
                name=tool,
                arguments_json=json.dumps({}),
            ),
        ),
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=200, completion_tokens=20),
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
        self.offered: list[frozenset[ToolId]] = []

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        self.offered.append(allowlist)
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


class FakeVcs:
    """Moves HEAD when told to, so the commit gate has something to observe."""

    def __init__(self, *, commits: int = 99) -> None:
        self.sha = "0" * 40
        self.remaining = commits
        self.diffs = 0

    async def head(self, worktree: Worktree) -> str:
        return self.sha

    async def diff(self, worktree: Worktree, revision: str = "HEAD") -> str:
        self.diffs += 1
        return (
            f"{self.sha}\nfeat: a change\n\nBody.\n\n"
            "diff --git a/src/a.py b/src/a.py\n+x = 1\n-y = 2\n"
        )

    def commit_lands(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1
            self.sha = f"{len(self.sha):040d}".replace("0", "a", 1) + str(self.remaining)


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


class CommittingTools(FakeTools):
    """A tool provider whose git_commit moves HEAD, like the real one does."""

    def __init__(self, vcs: FakeVcs) -> None:
        super().__init__()
        self._vcs = vcs

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        result = await super().call(accepted)
        if accepted.tool == ToolName.GIT_COMMIT:
            self._vcs.commit_lands()
        return result


def build(
    backend: FakeBackend,
    tools: FakeTools,
    sink: FakeSink,
    vcs: FakeVcs | None = None,
    *,
    max_revisions: int = 3,
    catalog: ToolCatalog | None = None,
) -> ImplementationRun:
    version_control = vcs or FakeVcs()
    return ImplementationRun(
        runner=StepRunner(backend=backend, tools=tools, events=sink),
        events=sink,
        tokenizer=backend,
        model="Qwen3-Coder-30B-A3B-Instruct",
        policy=CompactionPolicy(),
        vcs=version_control,  # type: ignore[arg-type]
        worktree=Worktree(path="/work", branch="harness/run-x", base_sha="0" * 40),
        catalog=catalog,
        max_revisions=max_revisions,
    )


def kinds(sink: FakeSink) -> list[str]:
    return [type(body).__name__ for body in sink.bodies]


def task_run(*, ack: bool = True) -> list[ChatResponse]:
    """One task's worth of responses: commit, report done, then the review.

    The review runs the tests before it answers, because a verdict that claims
    they passed without a green run in its own step is sent back once.
    """
    return [
        proposes_commit(),
        answer(),
        gathers("run_build"),
        gathers("run_tests"),
        verdict(ack=ack),
    ]


async def test_a_declared_server_is_logged_and_granted_to_the_named_roles() -> None:
    catalog = ToolCatalog(
        external=(
            ExternalTool(
                server="fpga",
                name="read_report",
                parameters={"type": "object"},
                roles=frozenset({"implementer"}),
            ),
        ),
    )
    vcs = FakeVcs()
    backend = FakeBackend(*task_run(), *task_run())
    tools, sink = CommittingTools(vcs), FakeSink()
    await build(backend, tools, sink, vcs, catalog=catalog).run(
        plan(),
        RunId("20261014-120000-abc123"),
        branch="harness/run-x",
        profile="cpu-only",
    )

    attached = [body for body in sink.bodies if isinstance(body, ev.ToolsAttached)]
    assert len(attached) == 1, "once per run, not once per step"
    assert attached[0].server == "fpga"
    assert attached[0].tools == ("read_report",)
    assert attached[0].roles == ("implementer",)

    # The implementer was granted it; the reviewer was not, and the gate is in
    # neither list, because the gate is the sandbox's.
    implementer, reviewer = tools.offered[0], tools.offered[1]
    assert "fpga__read_report" in implementer
    assert "fpga__read_report" not in reviewer
    assert ToolName.GIT_COMMIT in implementer


async def test_a_task_goes_implement_commit_review() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(*task_run(), *task_run())
    tools, sink = CommittingTools(vcs), FakeSink()
    result = await build(backend, tools, sink, vcs).run(
        plan(),
        RunId("20261014-120000-abc123"),
        branch="harness/run-x",
        profile="cpu-only",
    )
    assert result.completed
    assert [task.task_id for task in result.tasks] == ["t1", "t2"]
    first = result.tasks[0]
    assert [step.stage for step in first.steps] == ["implement", "review"]
    assert first.commits, "the commit gate saw a commit land"
    assert first.full_gate_pending, "the full tier has not run"
    assert kinds(sink)[:3] == ["RunStarted", "PlanCreated", "PlanApproved"]
    assert "VcsCommitted" in kinds(sink)
    assert kinds(sink)[-1] == "RunFinished"


async def test_a_task_that_never_commits_is_sent_to_fix_then_handed_back() -> None:
    vcs = FakeVcs()
    # The model reports done without ever calling git_commit, so HEAD never moves.
    backend = FakeBackend(*[answer() for _ in range(8)])
    sink = FakeSink()
    result = await build(backend, FakeTools(), sink, vcs, max_revisions=1).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    assert not result.completed
    task = result.tasks[0]
    assert task.stage is Stage.HANDOFF
    assert [step.stage for step in task.steps] == ["implement", "fix"]
    assert task.commits == ()
    assert "fix loop limit" in task.reason


async def test_a_nak_sends_the_task_to_the_fixer_with_the_notes() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(
        *task_run(ack=False),  # implement, commit, gather, gather, NAK
        proposes_commit(),  # fix
        answer(),
        gathers("run_build"),  # and the second review gathers again
        gathers("run_tests"),
        verdict(ack=True),
    )
    tools, sink = CommittingTools(vcs), FakeSink()
    result = await build(backend, tools, sink, vcs).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    task = result.tasks[0]
    assert task.finished
    assert task.revisions == 1
    assert [step.stage for step in task.steps] == ["implement", "review", "fix", "review"]
    fix_request = backend.requests[5]
    text = "\n".join(message.content for message in fix_request.messages)
    assert "Review: NAK" in text, "the fixer is told what the reviewer said"
    assert "the result of the inner call is dropped" in text
    assert "```diff" in text, "and sees the commit under review"


async def test_a_task_the_reviewer_keeps_rejecting_goes_back_to_the_human() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(
        *task_run(ack=False),
        proposes_commit(),
        answer(),
        verdict(ack=False),
        proposes_commit(),
        answer(),
        verdict(ack=False),
    )
    tools, sink = CommittingTools(vcs), FakeSink()
    result = await build(backend, tools, sink, vcs, max_revisions=1).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    task = result.tasks[0]
    assert not task.finished
    assert task.stage is Stage.HANDOFF
    assert task.revisions == 1
    assert "fix loop limit" in task.reason
    failures = [body for body in sink.bodies if isinstance(body, ev.StepFailed)]
    assert "the result of the inner call is dropped" in failures[0].diagnostics


async def test_the_reviewer_starts_from_the_diff() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(*task_run())
    await build(backend, CommittingTools(vcs), FakeSink(), vcs).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    review_request = backend.requests[2]
    text = "\n".join(message.content for message in review_request.messages)
    assert "```diff" in text
    assert "+x = 1" in text
    # The reviewer's turn offers tools, so it is not constrained to the
    # Verdict schema: a tool call would not satisfy it. The step declares the
    # artifact, and the constraint is applied on the turn that ends the step.
    assert review_request.response_format is None


async def test_the_run_stops_at_the_first_unfinished_task() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(answer(done=False))
    sink = FakeSink()
    result = await build(backend, FakeTools(), sink, vcs, max_revisions=0).run(
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


async def test_the_task_reaches_the_model_as_the_window_task() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(*task_run())
    await build(backend, CommittingTools(vcs), FakeSink(), vcs).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    contents = [message.content for message in backend.requests[0].messages]
    assert any("Task 1 of 1" in content for content in contents)
    assert any("do thing 1" in content for content in contents)
    assert any("NOTES.md" in content for content in contents)
    assert backend.requests[0].response_format is None, "this turn may call a tool"


async def test_a_committing_task_goes_through_the_tool() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(*task_run())
    tools, sink = CommittingTools(vcs), FakeSink()
    await build(backend, tools, sink, vcs).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    assert [call.tool for call in tools.calls] == [
        ToolName.GIT_COMMIT,
        # The reviewer's own two, because a verdict claiming they passed is
        # sent back once when nothing in its step ran them.
        ToolName.RUN_BUILD,
        ToolName.RUN_TESTS,
    ]
    assert "ToolCalled" in kinds(sink)
    assert "ToolReturned" in kinds(sink)


async def test_the_event_stream_records_where_the_tools_ran() -> None:
    vcs = FakeVcs()
    sink = FakeSink()
    await build(FakeBackend(*task_run()), CommittingTools(vcs), sink, vcs).run(
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


class FakeControl:
    """A control inbox that hands out prepared commands once."""

    def __init__(self, *commands: ControlCommand) -> None:
        self._commands = list(commands)
        self.drains = 0

    async def submit(self, command: ControlCommand) -> ControlMessage:  # pragma: no cover
        raise NotImplementedError

    async def drain(self) -> tuple[ControlMessage, ...]:
        self.drains += 1
        if not self._commands:
            return ()
        pending = self._commands.pop(0)
        now = datetime.now(UTC)
        return (ControlMessage(id=message_id(pending, now), ts=now, command=pending),)

    async def pending(self) -> int:  # pragma: no cover - not used here
        return len(self._commands)


def build_with_control(
    backend: FakeBackend,
    tools: FakeTools,
    sink: FakeSink,
    vcs: FakeVcs,
    control: FakeControl,
) -> ImplementationRun:
    return ImplementationRun(
        runner=StepRunner(backend=backend, tools=tools, events=sink),
        events=sink,
        tokenizer=backend,
        model="m",
        policy=CompactionPolicy(),
        vcs=vcs,  # type: ignore[arg-type]
        worktree=Worktree(path="/work", branch="b", base_sha="0" * 40),
        control=control,
    )


async def test_a_cancel_from_another_driver_stops_the_run() -> None:
    vcs = FakeVcs()
    control = FakeControl(
        CancelRun(run_id=RunId("r1"), reason="changed my mind", source=Driver.MCP)
    )
    sink = FakeSink()
    result = await build_with_control(
        FakeBackend(*task_run(), *task_run()),
        CommittingTools(vcs),
        sink,
        vcs,
        control,
    ).run(plan(), RunId("r1"), branch="b", profile="cpu-only")

    assert result.outcome is RunOutcome.CANCELLED
    assert "RunCancelled" in kinds(sink)
    cancelled = [body for body in sink.bodies if isinstance(body, ev.RunCancelled)]
    assert cancelled[0].source == "mcp"
    finished = [body for body in sink.bodies if isinstance(body, ev.RunFinished)]
    assert finished[0].outcome is RunOutcome.CANCELLED
    assert "changed my mind" in finished[0].reason


async def test_a_cancel_for_another_run_is_ignored() -> None:
    vcs = FakeVcs()
    control = FakeControl(CancelRun(run_id=RunId("someone-else"), source=Driver.TUI))
    result = await build_with_control(
        FakeBackend(*task_run()),
        CommittingTools(vcs),
        FakeSink(),
        vcs,
        control,
    ).run(plan(tasks=1), RunId("r1"), branch="b", profile="cpu-only")
    assert result.completed


async def test_an_approval_from_the_inbox_is_recorded_as_an_event() -> None:
    vcs = FakeVcs()
    control = FakeControl(ApprovePlan(plan_digest=plan().digest(), source=Driver.MCP))
    sink = FakeSink()
    await build_with_control(
        FakeBackend(*task_run()),
        CommittingTools(vcs),
        sink,
        vcs,
        control,
    ).run(plan(tasks=1), RunId("r1"), branch="b", profile="cpu-only")
    approvals = [body for body in sink.bodies if isinstance(body, ev.PlanApproved)]
    assert len(approvals) == 2, "the run's own approval, and the one from the inbox"


def test_detaching_re_invokes_this_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    class FakeProcess:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            recorded.append(argv)

    monkeypatch.setattr("niharness.management.runs.subprocess.Popen", FakeProcess)
    config = HarnessConfig.load(tmp_path)
    run_id = RunRegistry(config).start_detached(
        new_run_id(plan(tasks=1)),
        plan(tasks=1),
        local_tools=True,
    )
    assert recorded, "a child was started"
    argv = recorded[0]
    assert argv[0].endswith("python") or "python" in argv[0]
    assert argv[1:4] == ["-m", "niharness", "implement"]
    assert "--run-id" in argv
    assert argv[argv.index("--run-id") + 1] == run_id
    assert "--unsafe-local-tools" in argv
    assert "--detach" not in argv, "the child must not detach again"
    assert (config.runs_dir / run_id / "implement.log").exists()


async def test_a_finished_task_is_recorded_in_the_log() -> None:
    vcs = FakeVcs()
    sink = FakeSink()
    await build(FakeBackend(*task_run()), CommittingTools(vcs), sink, vcs).run(
        plan(tasks=1),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
    )
    concluded = [body for body in sink.bodies if isinstance(body, ev.TaskFinished)]
    assert len(concluded) == 1
    assert concluded[0].task_id == "t1"
    assert concluded[0].outcome == "completed"
    assert concluded[0].commits, "the commits of the task are in its conclusion"


async def test_a_handed_back_task_says_so_in_the_log() -> None:
    vcs = FakeVcs()
    sink = FakeSink()
    await build(
        FakeBackend(*[answer() for _ in range(8)]),
        FakeTools(),
        sink,
        vcs,
        max_revisions=0,
    ).run(plan(tasks=1), RunId("r1"), branch="b", profile="cpu-only")
    concluded = [body for body in sink.bodies if isinstance(body, ev.TaskFinished)]
    assert concluded[0].outcome == "handoff"


async def test_a_resumed_run_skips_what_it_already_did() -> None:
    vcs = FakeVcs()
    backend = FakeBackend(*task_run())
    sink = FakeSink()
    result = await build(backend, CommittingTools(vcs), sink, vcs).run(
        plan(tasks=2),
        RunId("r1"),
        branch="b",
        profile="cpu-only",
        done=("t1",),
        resumed_from=42,
    )
    assert result.completed
    assert [task.task_id for task in result.tasks] == ["t2"], "t1 was not run again"
    resumed = [body for body in sink.bodies if isinstance(body, ev.RunResumed)]
    assert resumed[0].from_seq == 42
    assert kinds(sink).index("RunResumed") < kinds(sink).index("RunStarted")


async def test_a_commit_the_gate_stage_never_saw_is_still_recorded() -> None:
    """Measured on L3: a fix step committed green and the task reported nothing.

    The commit-gate stage is the only place that compared HEAD, so a step that
    committed and then ran out of budget before answering went to handoff with
    the work sitting in git and `task.finished` saying none. §4 makes that
    event what `nih resume` reads to skip work already committed, so a missed
    commit is work done twice.
    """
    vcs = FakeVcs()
    # Commits, then never answers: the step ends on its budget, so the stage
    # machine goes to handoff without the commit gate running again.
    backend = FakeBackend(
        proposes_commit(),
        gathers("run_lint"),
        gathers("run_lint"),
        gathers("run_lint"),
        gathers("run_lint"),
    )
    tools, sink = CommittingTools(vcs), FakeSink()
    result = await build(backend, tools, sink, vcs).run(
        plan(), RunId("r"), branch="harness/run-x", profile="p", tools="local"
    )

    assert result.outcome is RunOutcome.FAILED, "the task did not get through review"
    task = result.tasks[-1]
    assert task.commits, "the commit landed, so the task says so"
    assert vcs.sha in task.commits
    finished = next(body for body in sink.bodies if isinstance(body, ev.TaskFinished))
    assert finished.commits == task.commits, "and the event a resume reads agrees"

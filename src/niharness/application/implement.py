# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Executing an approved plan, task by task.

One task is one logical change and therefore one commit. The implementer works
through the tool server; the commit it attempts is the gate, so this layer does
not check the result of the work - it could not do so more strictly than the
gate already does. What it owns is the shape of a run: a window seeded from the
plan, one step per task, the events that record it, and stopping when a task
comes back unfinished rather than marching on to the next one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from niharness.application.step_runner import StepRunner
from niharness.application.summarizer import Summarizer
from niharness.application.window import Window
from niharness.domain import events as ev
from niharness.domain.artifacts import Decision, FinalAnswer, Plan, Task, Verdict
from niharness.domain.budgets import fit_to_host
from niharness.domain.context import CompactionPolicy
from niharness.domain.control import AnswerQuestions, ApprovePlan, CancelRun
from niharness.domain.events import RunOutcome, StopReason
from niharness.domain.identity import RunId, StepId, derive_id, digest_json
from niharness.domain.loop import LoopThresholds
from niharness.domain.messages import ResponseFormat
from niharness.domain.pipeline import DEFAULT_MAX_REVISIONS, Outcome, Stage, advance, step_for
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig
from niharness.domain.tools import EMPTY_CATALOG, ToolCatalog, ToolLimits
from niharness.ports.control_channel import ControlChannel
from niharness.ports.event_sink import EventSink
from niharness.ports.tokenizer import Tokenizer
from niharness.ports.vcs import Vcs, Worktree
from niharness.prompts import assemble

NOTES_POINTER = (
    "NOTES.md is where anything blocked, uncertain or out of reach goes. Write "
    "there instead of working around a constraint."
)
"""The priority-two reminder every implementing step carries."""

_log = structlog.get_logger(__name__)


class StepResult(BaseModel):
    """What one step of one task did."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: str
    step_id: StepId
    stop_reason: StopReason
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    answer: FinalAnswer | None = None
    verdict: Verdict | None = None

    @property
    def finished(self) -> bool:
        """True when the model stopped because it was done, with a filled checklist."""
        return (
            self.stop_reason is StopReason.FINAL_ANSWER
            and self.answer is not None
            and self.answer.complete
        )


class TaskResult(BaseModel):
    """What happened to one task, across however many steps it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    stage: Stage = Field(description="Where the task ended: completed, or handed back.")
    reason: str = ""
    revisions: int = Field(default=0, ge=0)
    steps: tuple[StepResult, ...] = ()
    commits: tuple[str, ...] = ()
    full_gate_pending: bool = Field(
        default=False,
        description="The reviewer acknowledged it; the full tier has not run.",
    )

    @property
    def finished(self) -> bool:
        """True when the task got through review."""
        return self.stage is Stage.COMPLETED

    @property
    def tool_calls(self) -> int:
        """Tool calls across every step of this task."""
        return sum(step.tool_calls for step in self.steps)

    @property
    def tokens(self) -> int:
        """Tokens across every step of this task."""
        return sum(step.tokens for step in self.steps)


class RunResult(BaseModel):
    """What happened to a run of a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    outcome: RunOutcome
    tasks: tuple[TaskResult, ...] = ()
    stopped_at: str = Field(default="", description="The task that ended the run, if one did.")

    @property
    def completed(self) -> bool:
        """True when every task finished."""
        return self.outcome is RunOutcome.COMPLETED


def render_task(task: Task, plan: Plan, position: int) -> str:
    """Render one task as the window's priority-one statement of work."""
    lines = [
        f"# Task {position} of {len(plan.tasks)}: {task.title}",
        "",
        f"- id: `{task.id}`",
        f"- intent: {task.intent}",
        f"- expected commit type: `{task.commit_type}`",
    ]
    if task.satisfies:
        lines.append(f"- satisfies acceptance criteria: {', '.join(task.satisfies)}")
    if task.files:
        lines.append(f"- files the planner expected to touch: {', '.join(task.files)}")
    if task.depends_on:
        lines.append(f"- builds on: {', '.join(task.depends_on)} (already done in this run)")
    lines += [
        "",
        "Do this task and nothing else. Commit it through `git_commit`: that is "
        "the gate, and a red gate means there is no commit.",
    ]
    return "\n".join(lines)


class ImplementationRun:
    """Runs an approved plan in a worktree, one step per task."""

    def __init__(
        self,
        *,
        runner: StepRunner,
        events: EventSink,
        tokenizer: Tokenizer,
        model: str,
        policy: CompactionPolicy | None = None,
        thresholds: LoopThresholds | None = None,
        limits: ToolLimits | None = None,
        vcs: Vcs,
        worktree: Worktree,
        summarizer: Summarizer | None = None,
        control: ControlChannel | None = None,
        catalog: ToolCatalog | None = None,
        generation_tps: float = 0.0,
        max_revisions: int = DEFAULT_MAX_REVISIONS,
    ) -> None:
        """Wire what a run needs beyond the step executor.

        Args:
            runner: the step executor, already holding the ports.
            events: the run's event sink.
            tokenizer: measures every window, through the backend.
            model: the model name requested for implementing steps.
            policy: compaction policy; the shipped defaults when omitted.
            thresholds: loop detector thresholds.
            limits: tool argument and result caps.
            summarizer: the utility model, for compaction summaries.
            vcs: version control. A run needs it: the commit gate is "did a
                commit land", and the reviewer starts from the diff.
            worktree: the run's worktree.
            control: the inbox other drivers submit to - a cancel from the TUI
                and a cancel from an agent arrive the same way.
            catalog: the external MCP tools this run reached, and the roles
                they were granted to; empty unless the project declared a
                server.
            generation_tps: measured generation throughput of the main model.
                A step's token budget is fitted to what its wall-clock budget
                can pay for at this speed; zero means unmeasured and leaves
                the shipped budgets alone.
            max_revisions: how many fix rounds a task gets before it goes back
                to the human.
        """
        self._runner = runner
        self._events = events
        self._tokenizer = tokenizer
        self._model = model
        self._policy = policy or CompactionPolicy()
        self._thresholds = thresholds or LoopThresholds()
        self._limits = limits or ToolLimits()
        self._summarizer = summarizer
        self._vcs = vcs
        self._worktree = worktree
        self._control = control
        self._catalog = catalog or EMPTY_CATALOG
        self._generation_tps = generation_tps
        self._max_revisions = max_revisions
        self._cancelled: str | None = None

    async def run(
        self,
        plan: Plan,
        run_id: RunId,
        *,
        branch: str,
        profile: str,
        tools: Literal["sandbox", "local"] = "sandbox",
        sandbox_network: Literal["none", "private"] = "none",
        done: Sequence[str] = (),
        resumed_from: int = 0,
    ) -> RunResult:
        """Execute the plan's tasks, stopping at the first one that does not finish.

        Args:
            plan: the approved plan.
            run_id: this run.
            branch: the worktree's branch.
            profile: the hardware profile in force.
            tools: where the tool server ran.
            sandbox_network: whether the sandbox had a network. Recorded,
                because a run whose tools could reach the internet is a
                different run from one whose could not.
            done: tasks a previous attempt at this run already completed; they
                are skipped, because their commits are already on the branch.
            resumed_from: the event this run continues after, when it is a resume.

        Returns:
            What happened to each task that ran.
        """
        if resumed_from:
            await self._events.append(ev.RunResumed(from_seq=resumed_from))
        await self._events.append(
            ev.RunStarted(
                branch=branch,
                profile=profile,
                model=self._model,
                plan_digest=plan.digest(),
                tools=tools,
                sandbox_network=sandbox_network,
            ),
        )
        await self._events.append(
            ev.PlanCreated(
                plan_digest=plan.digest(), task_count=len(plan.tasks), imported=True
            ),
        )
        await self._events.append(ev.PlanApproved(plan_digest=plan.digest()))
        for server in self._catalog.servers():
            await self._events.append(
                ev.ToolsAttached(
                    server=server,
                    tools=self._catalog.tools_of(server),
                    roles=self._catalog.roles_of(server),
                ),
            )

        finished = set(done)
        results: list[TaskResult] = []
        for position, task in enumerate(plan.tasks, start=1):
            if task.id in finished:
                _log.info("task.skipped", task=task.id, reason="already done in this run")
                continue
            if await self._stop_requested(run_id) is not None:
                break
            result = await self._run_task(task, plan, position, run_id)
            results.append(result)
            if self._cancelled is not None:
                await self._finish(RunOutcome.CANCELLED, self._cancelled)
                return RunResult(
                    run_id=run_id,
                    outcome=RunOutcome.CANCELLED,
                    tasks=tuple(results),
                    stopped_at=task.id,
                )
            if not result.finished:
                await self._finish(RunOutcome.FAILED, f"task {task.id} did not finish")
                return RunResult(
                    run_id=run_id,
                    outcome=RunOutcome.FAILED,
                    tasks=tuple(results),
                    stopped_at=task.id,
                )
        if self._cancelled is not None:
            await self._finish(RunOutcome.CANCELLED, self._cancelled)
            return RunResult(run_id=run_id, outcome=RunOutcome.CANCELLED, tasks=tuple(results))
        await self._finish(RunOutcome.COMPLETED, f"{len(results)} task(s) done")
        return RunResult(run_id=run_id, outcome=RunOutcome.COMPLETED, tasks=tuple(results))

    async def _finish(self, outcome: RunOutcome, reason: str) -> None:
        """Record the run's terminal event."""
        await self._events.append(ev.RunFinished(outcome=outcome, reason=reason))
        _log.info("run.finished", outcome=outcome.value, reason=reason)

    async def _run_task(
        self,
        task: Task,
        plan: Plan,
        position: int,
        run_id: RunId,
    ) -> TaskResult:
        """Run one task through implement, the commit gate, review and any fixes."""
        stage = Stage.IMPLEMENT
        revision = 0
        steps: list[StepResult] = []
        commits: list[str] = []
        verdict: Verdict | None = None
        head = await self._head()
        _log.info("task.started", task=task.id, position=position, of=len(plan.tasks))

        while True:
            if stage in {Stage.IMPLEMENT, Stage.FIX}:
                step = await self._run_step(
                    stage, task, plan, position, run_id, revision, verdict
                )
                steps.append(step)
                outcome = Outcome.DONE if step.finished else Outcome.FAILED
            elif stage is Stage.COMMIT_GATE:
                landed = await self._landed(head, run_id, task)
                head = landed or head
                outcome = Outcome.GREEN if landed else Outcome.RED
                if landed:
                    commits.append(landed)
            elif stage is Stage.REVIEW:
                step = await self._run_step(
                    stage, task, plan, position, run_id, revision, verdict
                )
                steps.append(step)
                verdict = step.verdict
                outcome = _review_outcome(verdict)
            else:  # Stage.FULL_GATE - the task is done as far as this run goes
                return await self._conclude(
                    TaskResult(
                        task_id=task.id,
                        stage=Stage.COMPLETED,
                        reason="reviewer ACK; the full gate tier has not run",
                        revisions=revision,
                        steps=tuple(steps),
                        commits=tuple(commits),
                        full_gate_pending=True,
                    ),
                )

            transition = advance(stage, outcome, revision, self._max_revisions)
            _log.info(
                "task.stage",
                task=task.id,
                stage=stage.value,
                outcome=outcome.value,
                next=transition.stage.value,
                revision=transition.revision,
            )
            if transition.terminal and transition.stage is not Stage.COMPLETED:
                await self._events.append(
                    ev.StepFailed(
                        reason=f"task {task.id}: {transition.reason}",
                        diagnostics=_diagnostics(steps[-1] if steps else None, verdict),
                    ),
                )
                return await self._conclude(
                    TaskResult(
                        task_id=task.id,
                        stage=transition.stage,
                        reason=transition.reason,
                        revisions=transition.revision,
                        steps=tuple(steps),
                        commits=tuple(commits),
                    ),
                )
            stage, revision = transition.stage, transition.revision

    async def _stop_requested(self, run_id: RunId) -> str | None:
        """Drain the control inbox and return a reason to stop, if one arrived.

        Every command is turned into an event here, by the run, so the log keeps
        its single writer and records what the run acted on rather than what was
        merely asked for.
        """
        if self._control is None:
            return self._cancelled
        for message in await self._control.drain():
            command = message.command
            if isinstance(command, CancelRun) and command.run_id == run_id:
                self._cancelled = command.reason or f"cancelled from {command.source.value}"
                await self._events.append(ev.RunCancelled(source=command.source.value))
                _log.warning("run.cancel_requested", run_id=run_id, source=command.source.value)
            elif isinstance(command, ApprovePlan):
                await self._events.append(ev.PlanApproved(plan_digest=command.plan_digest))
            elif isinstance(command, AnswerQuestions) and command.run_id == run_id:
                await self._events.append(
                    ev.QuestionsAnswered(
                        answers_digest=digest_json(
                            [answer.model_dump(mode="json") for answer in command.answers],
                        ),
                        count=len(command.answers),
                    ),
                )
        return self._cancelled

    async def _conclude(self, result: TaskResult) -> TaskResult:
        """Record what the run concluded about a task, then return it."""
        await self._events.append(
            ev.TaskFinished(
                task_id=result.task_id,
                outcome="completed" if result.finished else "handoff",
                revisions=result.revisions,
                commits=result.commits,
                reason=result.reason,
            ),
        )
        return result

    async def _head(self) -> str:
        """Return the worktree's current commit."""
        return await self._vcs.head(self._worktree)

    async def _landed(self, before: str, run_id: RunId, task: Task) -> str:
        """Return the new commit if one landed, recording it in the event stream.

        The commit itself happens inside the tool server, where the gate runs.
        The harness notices it here, so the run's log says what was committed
        even though it did not do the committing.
        """
        del run_id
        head = await self._vcs.head(self._worktree)
        if head == before:
            return ""
        diff = await self._vcs.diff(self._worktree)
        await self._events.append(_commit_event(head, diff))
        _log.info("task.committed", task=task.id, sha=head[:12])
        return head

    async def _run_step(
        self,
        stage: Stage,
        task: Task,
        plan: Plan,
        position: int,
        run_id: RunId,
        revision: int,
        verdict: Verdict | None,
    ) -> StepResult:
        """Run one model step for `stage`."""
        role = STAGE_ROLES[stage]
        spec = DEFAULT_ROLES[role]
        definition = step_for(stage)
        kind = STAGE_ARTIFACTS[stage]
        step_id = StepId(derive_id(run_id, stage.value, task.id, str(revision)))
        window = Window(self._tokenizer, self._policy, summarizer=self._summarizer)
        await window.add_prompt(assemble(spec.prompt_layers))
        await window.add_task(render_task(task, plan, position))
        await window.add_notes(NOTES_POINTER)
        for extra in await self._context_for(stage, verdict):
            await window.add_notes(extra)

        config = LoopConfig(
            stage=stage.value,
            role=role,
            model=self._model,
            allowlist=spec.allowlist | self._catalog.allowlist_for(role.value),
            catalog=self._catalog,
            budgets=fit_to_host(definition.budgets, self._generation_tps),
            thresholds=self._thresholds,
            limits=self._limits,
            task_id=task.id,
            temperature=spec.temperature,
            final_artifact=kind,
            response_format=ResponseFormat(
                name=kind.value,
                json_schema=ARTIFACT_SCHEMAS[kind](),
            ),
        )
        state = await self._runner.run(
            config,
            window,
            step_id=step_id,
            should_stop=(lambda: self._stop_requested(run_id)) if self._control else None,
        )
        return StepResult(
            stage=stage.value,
            step_id=step_id,
            stop_reason=state.stop_reason or StopReason.ERROR,
            tool_calls=state.budget.tool_calls_used,
            tokens=state.budget.tokens_used,
            answer=state.final_answer,
            verdict=state.verdict,
        )

    async def _context_for(self, stage: Stage, verdict: Verdict | None) -> tuple[str, ...]:
        """Return what this stage needs beyond the task: the diff, the notes."""
        extra: list[str] = []
        if stage in {Stage.REVIEW, Stage.FIX}:
            diff = await self._vcs.diff(self._worktree)
            extra.append(f"The commit under review:\n\n```diff\n{diff.strip()}\n```")
        if stage is Stage.FIX and verdict is not None:
            extra.append(render_verdict(verdict))
        return tuple(extra)


STAGE_ROLES: dict[Stage, Role] = {
    Stage.IMPLEMENT: Role.IMPLEMENTER,
    Stage.FIX: Role.FIXER,
    Stage.REVIEW: Role.REVIEWER,
}
"""Which role runs which stage."""

STAGE_ARTIFACTS: dict[Stage, ArtifactKind] = {
    Stage.IMPLEMENT: ArtifactKind.FINAL_ANSWER,
    Stage.FIX: ArtifactKind.FINAL_ANSWER,
    Stage.REVIEW: ArtifactKind.VERDICT,
}
"""Which artifact ends which stage."""

ARTIFACT_SCHEMAS: dict[ArtifactKind, Callable[[], dict[str, Any]]] = {
    ArtifactKind.FINAL_ANSWER: FinalAnswer.model_json_schema,
    ArtifactKind.VERDICT: Verdict.model_json_schema,
}
"""How each artifact's schema is produced for structured output."""


def render_verdict(verdict: Verdict) -> str:
    """Render a reviewer's verdict as the fixer's instructions."""
    lines = [f"# Review: {verdict.decision.value}", "", verdict.summary, ""]
    if verdict.notes:
        lines.append("Address every blocker. If you disagree with one, change nothing for it")
        lines.append("and say why in NOTES.md.")
        lines.append("")
        lines += [
            f"- `{note.file}`"
            + (f":{note.line}" if note.line else "")
            + f" [{note.severity.value}] {note.message}"
            for note in verdict.notes
        ]
    lines += [
        "",
        f"The reviewer ran the build ({'green' if verdict.build_passed else 'RED'}) "
        f"and the tests ({'green' if verdict.tests_passed else 'RED'}) itself.",
    ]
    return "\n".join(lines)


def _review_outcome(verdict: Verdict | None) -> Outcome:
    """Map a verdict onto the pipeline's outcome, treating a missing one as failure."""
    if verdict is None:
        return Outcome.FAILED
    return Outcome.ACK if verdict.decision is Decision.ACK else Outcome.NAK


def _commit_event(sha: str, diff: str) -> ev.VcsCommitted:
    """Derive the commit event from the diff the adapter returned."""
    lines = diff.splitlines()
    subject = lines[1].strip() if len(lines) > 1 else ""
    files = sum(1 for line in lines if line.startswith("diff --git "))
    insertions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return ev.VcsCommitted(
        sha=sha,
        subject=subject,
        files_changed=files,
        insertions=insertions,
        deletions=deletions,
    )


def _diagnostics(step: StepResult | None, verdict: Verdict | None) -> str:
    """Return what the human needs to pick the task up."""
    if verdict is not None and verdict.decision is Decision.NAK:
        blockers = "; ".join(note.message for note in verdict.notes)
        return f"reviewer: {verdict.summary}" + (f" | blockers: {blockers}" if blockers else "")
    if step is None or step.answer is None:
        return "the step produced no final answer"
    unmet = [item.statement for item in step.answer.dod if not item.done]
    if not unmet:
        return step.answer.summary
    return f"{step.answer.summary}\nnot done: " + "; ".join(unmet)


def task_order(plan: Plan) -> Sequence[str]:
    """Return the plan's task ids, in execution order."""
    return [task.id for task in plan.tasks]

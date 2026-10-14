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

from collections.abc import Sequence
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from niharness.application.step_runner import StepRunner
from niharness.application.summarizer import Summarizer
from niharness.application.window import Window
from niharness.domain import events as ev
from niharness.domain.artifacts import FinalAnswer, Plan, Task
from niharness.domain.context import CompactionPolicy
from niharness.domain.events import RunOutcome, StopReason
from niharness.domain.identity import RunId, StepId, derive_id
from niharness.domain.loop import LoopThresholds
from niharness.domain.messages import ResponseFormat
from niharness.domain.pipeline import Stage, step_for
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import LoopConfig
from niharness.domain.tools import ToolLimits
from niharness.ports.event_sink import EventSink
from niharness.ports.tokenizer import Tokenizer
from niharness.prompts import assemble

NOTES_POINTER = (
    "NOTES.md is where anything blocked, uncertain or out of reach goes. Write "
    "there instead of working around a constraint."
)
"""The priority-two reminder every implementing step carries."""

_log = structlog.get_logger(__name__)


class TaskResult(BaseModel):
    """What happened to one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    step_id: StepId
    stop_reason: StopReason
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    answer: FinalAnswer | None = None

    @property
    def finished(self) -> bool:
        """True when the model stopped because it was done, with a filled checklist."""
        return (
            self.stop_reason is StopReason.FINAL_ANSWER
            and self.answer is not None
            and self.answer.complete
        )


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
        summarizer: Summarizer | None = None,
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
        """
        self._runner = runner
        self._events = events
        self._tokenizer = tokenizer
        self._model = model
        self._policy = policy or CompactionPolicy()
        self._thresholds = thresholds or LoopThresholds()
        self._limits = limits or ToolLimits()
        self._summarizer = summarizer

    async def run(
        self,
        plan: Plan,
        run_id: RunId,
        *,
        branch: str,
        profile: str,
        tools: Literal["sandbox", "local"] = "sandbox",
    ) -> RunResult:
        """Execute every task of `plan`, stopping at the first one that does not finish."""
        await self._events.append(
            ev.RunStarted(
                branch=branch,
                profile=profile,
                model=self._model,
                plan_digest=plan.digest(),
                tools=tools,
            ),
        )
        await self._events.append(
            ev.PlanCreated(
                plan_digest=plan.digest(), task_count=len(plan.tasks), imported=True
            ),
        )
        await self._events.append(ev.PlanApproved(plan_digest=plan.digest()))

        results: list[TaskResult] = []
        for position, task in enumerate(plan.tasks, start=1):
            result = await self._run_task(task, plan, position, run_id)
            results.append(result)
            if not result.finished:
                await self._finish(RunOutcome.FAILED, f"task {task.id} did not finish")
                return RunResult(
                    run_id=run_id,
                    outcome=RunOutcome.FAILED,
                    tasks=tuple(results),
                    stopped_at=task.id,
                )
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
        """Run one implementing step for `task`."""
        spec = DEFAULT_ROLES[Role.IMPLEMENTER]
        step = step_for(Stage.IMPLEMENT)
        step_id = StepId(derive_id(run_id, Stage.IMPLEMENT.value, task.id))
        window = Window(self._tokenizer, self._policy, summarizer=self._summarizer)
        await window.add_prompt(assemble(spec.prompt_layers))
        await window.add_task(render_task(task, plan, position))
        await window.add_notes(NOTES_POINTER)

        config = LoopConfig(
            stage=Stage.IMPLEMENT.value,
            role=Role.IMPLEMENTER,
            model=self._model,
            allowlist=spec.allowlist,
            budgets=step.budgets,
            thresholds=self._thresholds,
            limits=self._limits,
            task_id=task.id,
            temperature=spec.temperature,
            response_format=ResponseFormat(
                name="FinalAnswer",
                json_schema=FinalAnswer.model_json_schema(),
            ),
        )
        _log.info("task.started", task=task.id, position=position, of=len(plan.tasks))
        state = await self._runner.run(config, window, step_id=step_id)
        result = TaskResult(
            task_id=task.id,
            step_id=step_id,
            stop_reason=state.stop_reason or StopReason.ERROR,
            tool_calls=state.budget.tool_calls_used,
            tokens=state.budget.tokens_used,
            answer=state.final_answer,
        )
        if not result.finished:
            await self._events.append(
                ev.StepFailed(
                    reason=f"task {task.id}: {result.stop_reason.value}",
                    diagnostics=_diagnostics(state.final_answer),
                ),
                step_id=step_id,
            )
        _log.info(
            "task.finished",
            task=task.id,
            finished=result.finished,
            stop_reason=result.stop_reason.value,
            tool_calls=result.tool_calls,
        )
        return result


def _diagnostics(answer: FinalAnswer | None) -> str:
    """Return what the human needs to pick the task up, from an unfinished answer."""
    if answer is None:
        return "the step produced no final answer"
    unmet = [item.statement for item in answer.dod if not item.done]
    if not unmet:
        return answer.summary
    return f"{answer.summary}\nnot done: " + "; ".join(unmet)


def task_order(plan: Plan) -> Sequence[str]:
    """Return the plan's task ids, in execution order."""
    return [task.id for task in plan.tasks]

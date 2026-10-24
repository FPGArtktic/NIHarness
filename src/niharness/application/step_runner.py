# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The executor for one step.

The tool loop decides; this performs. Each iteration asks the domain what
should happen, does exactly that through a port, and folds the outcome back:

    action = next_action(state)
    ...                             # a model call, a tool call, a rollback
    step = observe_*(state, outcome)
    append(step.events)

Nothing here decides whether a call is allowed, whether a step is looping, or
when a budget is spent. Those are the domain's, which is why this module has
no branches a test would want to pin beyond "it did what it was told".
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog

from niharness.application.window import Window
from niharness.domain import events as ev
from niharness.domain.gate import output_digest
from niharness.domain.identity import StepId, digest_json
from niharness.domain.messages import ChatRequest, ToolChoice, ToolSchema
from niharness.domain.tool_loop import (
    CallModel,
    ExecuteTool,
    InjectResult,
    LoopConfig,
    LoopState,
    Rollback,
    Step,
    Stop,
    cancel,
    degenerate_response,
    injected,
    next_action,
    observe_model,
    observe_tool,
    restarted,
    start,
)
from niharness.domain.tools import ToolName
from niharness.ports.event_sink import EventSink
from niharness.ports.model_backend import ModelBackend
from niharness.ports.tool_provider import ToolProvider
from niharness.ports.vcs import Vcs, Worktree

GATE_TOOLS: frozenset[ToolName] = frozenset(
    {ToolName.RUN_LINT, ToolName.RUN_BUILD, ToolName.RUN_TESTS, ToolName.GIT_COMMIT},
)
"""Tools whose failure output carries a gate signature the detectors compare."""

ITERATION_SLACK = 20
"""Head-room over the budget-derived iteration bound, for interventions and retries."""

_log = structlog.get_logger(__name__)


class RunnerLimitError(RuntimeError):
    """The executor ran more iterations than any budget could explain.

    The loop's stop conditions should always fire first; this is the guard that
    turns a hypothetical non-terminating loop into a loud failure instead of a
    quiet one.
    """


class StepRunner:
    """Runs one step to completion."""

    def __init__(
        self,
        *,
        backend: ModelBackend,
        tools: ToolProvider,
        events: EventSink,
        vcs: Vcs | None = None,
        worktree: Worktree | None = None,
        iteration_limit: int | None = None,
    ) -> None:
        """Wire the ports this executor drives.

        Args:
            backend: the inference endpoint.
            tools: the sandboxed tool provider.
            events: the run's event sink.
            vcs: version control, needed for worktree digests and rollbacks.
            worktree: the run's worktree; required when `vcs` is given.
            iteration_limit: overrides the budget-derived guard; tests use it to
                prove the guard fires rather than hanging.
        """
        self._backend = backend
        self._tools = tools
        self._events = events
        self._vcs = vcs
        self._worktree = worktree
        self._iteration_limit = iteration_limit

    async def run(
        self,
        config: LoopConfig,
        window: Window,
        *,
        step_id: StepId | None = None,
        should_stop: Callable[[], Awaitable[str | None]] | None = None,
    ) -> LoopState:
        """Run a step until the domain says to stop.

        Args:
            config: the step's role, budgets, allowlist and thresholds.
            window: the window, already seeded with the prompt layers, the task
                and any open notes. The executor appends to it and lets it
                compact itself before every model call.
            step_id: identifier stamped on this step's events.
            should_stop: asked once per iteration whether something outside the
                run wants it to stop; the reason, or None to carry on. A stop
                lands between actions, so the worktree is never left mid-tool.

        Returns:
            The final loop state, carrying the stop reason and, when there is
            one, the final answer.

        Raises:
            RunnerLimitError: if the loop somehow fails to terminate.
        """
        descriptors = await self._tools.descriptors(config.allowlist)
        step = start(config)
        await self._append(step, step_id)
        state = step.state

        budget = config.budgets
        limit = self._iteration_limit or (budget.max_tool_calls + 1) * 4 + ITERATION_SLACK
        for _ in range(limit):
            if should_stop is not None:
                reason = await should_stop()
                if reason is not None:
                    _log.info("step.cancelled", stage=config.stage, reason=reason)
                    stopped = cancel(state)
                    await self._append(stopped, step_id)
                    return stopped.state
            action = next_action(state)
            if isinstance(action, Stop):
                _log.info(
                    "step.stopped",
                    stage=config.stage,
                    reason=action.stop_reason.value,
                    tool_calls=state.budget.tool_calls_used,
                    tokens=state.budget.tokens_used,
                )
                return state
            if isinstance(action, CallModel):
                state = await self._call_model(state, action, window, descriptors, step_id)
            elif isinstance(action, ExecuteTool):
                state = await self._execute_tool(state, action, window, step_id)
            elif isinstance(action, InjectResult):
                await window.add_synthetic_result(action.call_id, action.text)
                state = injected(state).state
            elif isinstance(action, Rollback):
                state = await self._rollback(state, action, step_id)
        msg = f"step did not terminate within {limit} iterations"
        raise RunnerLimitError(msg)

    async def _append(self, step: Step, step_id: StepId | None) -> None:
        """Append every event a transition produced, in order."""
        for body in step.events:
            await self._events.append(body, step_id=step_id)

    async def _call_model(
        self,
        state: LoopState,
        action: CallModel,
        window: Window,
        descriptors: tuple[ToolSchema, ...],
        step_id: StepId | None,
    ) -> LoopState:
        """Ask the model, record the exchange, and fold the answer back."""
        config = state.config
        if action.instruction:
            await window.add_instruction(action.instruction)
        snapshot = await window.prepare()
        if snapshot.plan is not None:
            plan = snapshot.plan
            await self._events.append(
                ev.ContextCompacted(
                    before_tokens=plan.before_tokens,
                    after_tokens=plan.after_tokens,
                    deterministic_only=plan.deterministic_only,
                    summary_digest=window.summary_digest(snapshot.summary),
                    indexed_chunks=snapshot.archived,
                ),
                step_id=step_id,
            )
        offers_tools = action.tool_choice is not ToolChoice.NONE
        request = ChatRequest(
            model=config.model,
            messages=snapshot.messages,
            tools=descriptors if offers_tools else (),
            tool_choice=action.tool_choice,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            seed=config.seed,
            # The artifact's schema is demanded only on a turn that cannot call
            # a tool. Demanding it while tools are offered is a contradiction:
            # a tool call does not satisfy the artifact's schema, so the model
            # is told to produce something it is simultaneously forbidden to
            # produce. A real backend makes the contradiction loud - llama.cpp
            # installs a grammar for tool calls and another for the schema, and
            # refuses the request with "failed to parse grammar" - but the
            # contradiction was ours, and it is the reason this is a condition
            # rather than a workaround.
            # Not `offers_tools`: a reflection turn withholds tools and asks
            # for prose, and attaching the artifact's grammar to it demanded
            # two incompatible things at once. The loop says which turn wants
            # the artifact, because only the loop knows why the tools are off.
            response_format=config.response_format if action.expects_artifact else None,
        )
        await self._events.append(
            ev.ModelRequest(
                model=config.model,
                messages_digest=digest_json(
                    [message.model_dump(mode="json") for message in snapshot.messages],
                ),
                prompt_tokens=snapshot.tokens,
                window_fill=snapshot.fill,
                tool_choice=action.tool_choice.value,
            ),
            step_id=step_id,
        )
        started = time.perf_counter()
        response = await self._backend.chat(request)
        elapsed = time.perf_counter() - started
        # A degenerate answer never enters the window. The loop refuses it, but
        # refusing is too late on its own: the turn was recorded first, and the
        # *next* `prepare()` asked the backend to render a tool call whose JSON
        # had been truncated mid-string, which answered 500 and ended the run.
        # Asked here with the same pure function the loop uses, so the two
        # cannot disagree about what counts as degenerate.
        if degenerate_response(response, config.thresholds) is None:
            # Reasoning is logged as an event by the loop and deliberately left
            # out of the window: no field on a Message could carry it.
            await window.add_assistant(response.content, response.tool_calls)
        step = observe_model(state, response, seconds=elapsed)
        await self._append(step, step_id)
        return step.state

    async def _execute_tool(
        self,
        state: LoopState,
        action: ExecuteTool,
        window: Window,
        step_id: StepId | None,
    ) -> LoopState:
        """Run a validated call in the sandbox and fold the result back."""
        started = time.perf_counter()
        result = await self._tools.call(action.call)
        elapsed = time.perf_counter() - started
        await window.add_tool_result(result)
        signature = ""
        if not result.ok and result.tool in GATE_TOOLS:
            signature = str(output_digest(result.content))
        tree = None
        if self._vcs is not None and self._worktree is not None:
            tree = await self._vcs.tree_digest(self._worktree)
        step = observe_tool(
            state,
            result,
            tree_digest=tree,
            gate_signature=signature,
            wall_seconds=elapsed,
        )
        await self._append(step, step_id)
        return step.state

    async def _rollback(
        self,
        state: LoopState,
        action: Rollback,
        step_id: StepId | None,
    ) -> LoopState:
        """Reset the worktree to the last green commit and restart the step.

        Commits only land green, so the current HEAD is the last green state.
        Without version control there is nothing to roll back to, and the step
        restarts with its window cleared and the reason recorded.
        """
        if self._vcs is not None and self._worktree is not None:
            head = await self._vcs.head(self._worktree)
            await self._vcs.rollback(self._worktree, head)
            await self._events.append(
                ev.VcsRolledBack(to_sha=head, reason=action.reason),
                step_id=step_id,
            )
        _log.warning("step.restarted", stage=state.config.stage, reason=action.reason)
        return restarted(state).state

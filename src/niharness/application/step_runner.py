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

from collections.abc import Sequence

import structlog

from niharness.domain import events as ev
from niharness.domain.gate import output_digest
from niharness.domain.identity import StepId, digest_json
from niharness.domain.messages import (
    ChatRequest,
    Message,
    MessageRole,
    ToolChoice,
    ToolSchema,
)
from niharness.domain.tool_loop import (
    CallModel,
    ExecuteTool,
    InjectResult,
    LoopConfig,
    LoopState,
    Rollback,
    Step,
    Stop,
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
        seed: Sequence[Message],
        *,
        step_id: StepId | None = None,
    ) -> LoopState:
        """Run a step until the domain says to stop.

        Args:
            config: the step's role, budgets, allowlist and thresholds.
            seed: the initial window - prompt layers, the task, retrieved context.
            step_id: identifier stamped on this step's events.

        Returns:
            The final loop state, carrying the stop reason and, when there is
            one, the final answer.

        Raises:
            RunnerLimitError: if the loop somehow fails to terminate.
        """
        history: list[Message] = list(seed)
        descriptors = await self._tools.descriptors(config.allowlist)
        step = start(config)
        await self._append(step, step_id)
        state = step.state

        budget = config.budgets
        limit = self._iteration_limit or (budget.max_tool_calls + 1) * 4 + ITERATION_SLACK
        for _ in range(limit):
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
                state = await self._call_model(state, action, history, descriptors, step_id)
            elif isinstance(action, ExecuteTool):
                state = await self._execute_tool(state, action, history, step_id)
            elif isinstance(action, InjectResult):
                history.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=action.text,
                        tool_call_id=action.call_id,
                    ),
                )
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
        history: list[Message],
        descriptors: tuple[ToolSchema, ...],
        step_id: StepId | None,
    ) -> LoopState:
        """Ask the model, record the exchange, and fold the answer back."""
        config = state.config
        if action.instruction:
            history.append(Message(role=MessageRole.USER, content=action.instruction))
        offers_tools = action.tool_choice is not ToolChoice.NONE
        request = ChatRequest(
            model=config.model,
            messages=tuple(history),
            tools=descriptors if offers_tools else (),
            tool_choice=action.tool_choice,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            seed=config.seed,
            response_format=config.response_format,
        )
        await self._events.append(
            ev.ModelRequest(
                model=config.model,
                messages_digest=digest_json([m.model_dump(mode="json") for m in history]),
                prompt_tokens=state.budget.tokens_used,
                window_fill=0.0,
                tool_choice=action.tool_choice.value,
            ),
            step_id=step_id,
        )
        response = await self._backend.chat(request)
        # Reasoning is logged as an event by the loop and deliberately left out
        # of the history: there is no field on a Message that could carry it.
        history.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            ),
        )
        step = observe_model(state, response)
        await self._append(step, step_id)
        return step.state

    async def _execute_tool(
        self,
        state: LoopState,
        action: ExecuteTool,
        history: list[Message],
        step_id: StepId | None,
    ) -> LoopState:
        """Run a validated call in the sandbox and fold the result back."""
        result = await self._tools.call(action.call)
        history.append(
            Message(
                role=MessageRole.TOOL,
                content=result.content,
                tool_call_id=result.call_id,
                name=result.tool.value,
            ),
        )
        signature = ""
        if not result.ok and result.tool in GATE_TOOLS:
            signature = str(output_digest(result.content))
        tree = None
        if self._vcs is not None and self._worktree is not None:
            tree = await self._vcs.tree_digest(self._worktree)
        step = observe_tool(state, result, tree_digest=tree, gate_signature=signature)
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

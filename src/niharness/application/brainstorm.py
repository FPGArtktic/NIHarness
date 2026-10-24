# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry A: a conversation that ends in a `SPEC.md`.

Every other stage is one step: a window is built, a model answers, an artifact
is validated, the step ends. Brainstorming is a **conversation**, which is many
model calls over one window - and §4 says conversation history never crosses a
*stage* boundary, not that a stage may only call a model once.

So a session is a sequence of steps sharing one window and one budget. Each
turn is a step, which is what makes the detectors, the intervention ladder and
the budgets apply to it exactly as they apply to implementing work; and the
budget is the stage's, spent across the whole session, so a conversation cannot
run forever by being a conversation.

Nothing here writes a file. `/spec` asks for a `SpecDraft`, the harness binds
the origin it knows (ADR 0026), and the caller decides where it goes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from niharness.application.window import ContextArchive, Window
from niharness.domain import events as ev
from niharness.domain.artifacts import Spec, SpecDraft, SpecOrigin
from niharness.domain.budgets import Budgets, fit_to_host
from niharness.domain.context import CompactionPolicy
from niharness.domain.events import RunOutcome, StopReason
from niharness.domain.identity import RunId, StepId, derive_id
from niharness.domain.loop import LoopThresholds
from niharness.domain.pipeline import Stage, step_for
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig, LoopState
from niharness.domain.tools import EMPTY_CATALOG, ToolCatalog, ToolLimits
from niharness.prompts import assemble

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from niharness.application.step_runner import StepRunner
    from niharness.application.summarizer import Summarizer
    from niharness.ports.event_sink import EventSink
    from niharness.ports.tokenizer import Tokenizer

SESSION_NOTES = """This is a conversation, not a task. Nothing you say here reaches an
implementer except through the spec. You have read-only tools; you cannot change a file.

When the human writes `/spec` you will be asked for the spec itself, as a single
artifact, with no tools available on that turn. Everything that matters has to be in it."""
"""What the session tells the model about its own shape, once, at the start."""

CLOSING_INSTRUCTION = """Write the spec now, as the artifact and nothing else.

Every acceptance criterion needs a verification that can be run or observed - a command,
a test name, a condition. A criterion you cannot say how to check is not a criterion:
drop it or replace it. Do not include who wrote this; the harness knows."""
"""The closing turn's instruction. No tools, and the artifact's schema attached."""


class Turn(BaseModel):
    """What one exchange of the session produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1)
    said: str = Field(description="The model's prose, for the human to read.")
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    stop_reason: StopReason | None = None
    draft: SpecDraft | None = Field(
        default=None,
        description="Only the closing turn has one, and only if it validated.",
    )


class BrainstormResult(BaseModel):
    """What a session produced, and what it cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    outcome: RunOutcome
    turns: int = Field(default=0, ge=0)
    spec: Spec | None = None
    tool_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    reason: str = ""


class BrainstormSession:
    """One interactive session: many steps, one window, one budget."""

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
        archive: ContextArchive | None = None,
        catalog: ToolCatalog | None = None,
        generation_tps: float = 0.0,
    ) -> None:
        """Wire what the session needs beyond the step executor.

        The implementing run's arguments minus everything a read-only
        conversation has no use for: no version control, because nothing is
        committed, and no control channel, because the human is already here
        and can stop by saying so.

        Args:
            runner: the step executor, already holding the ports.
            events: the run's event sink.
            tokenizer: measures the window, through the backend.
            model: the model name this session runs on.
            policy: compaction policy; the shipped defaults when omitted.
            thresholds: loop detector thresholds.
            limits: tool argument caps.
            summarizer: makes a `ContextSummary` when the window fills. A long
                conversation is the one stage where that is routine.
            archive: indexes what compaction dropped, so `rag_query` can
                recover it.
            catalog: declared MCP servers, for the allowlist.
            generation_tps: measured throughput, for fitting the budget.
        """
        self._runner = runner
        self._events = events
        self._tokenizer = tokenizer
        self._model = model
        self._policy = policy or CompactionPolicy()
        self._thresholds = thresholds or LoopThresholds()
        self._limits = limits or ToolLimits()
        self._summarizer = summarizer
        self._archive = archive
        self._catalog = catalog or EMPTY_CATALOG
        self._generation_tps = generation_tps

        self._role = DEFAULT_ROLES[Role.BRAINSTORMER]
        self._budgets = fit_to_host(step_for(Stage.BRAINSTORM).budgets, generation_tps)
        self._run_id = RunId("")
        self._window: Window | None = None
        self._turns: list[Turn] = []
        self._spent_calls = 0
        self._spent_tokens = 0

    @property
    def turns(self) -> tuple[Turn, ...]:
        """Every exchange so far, oldest first."""
        return tuple(self._turns)

    @property
    def spent(self) -> tuple[int, int]:
        """Tool calls and tokens the session has used."""
        return self._spent_calls, self._spent_tokens

    @property
    def exhausted(self) -> bool:
        """Whether the session has spent the stage's budget.

        The budget belongs to the stage, not to a turn, so a conversation
        cannot outrun it by being a conversation. When it is gone the session
        can still be asked for the spec: that turn calls no tool and the point
        of a budget is not to end a session with nothing written.
        """
        return (
            self._spent_calls >= self._budgets.max_tool_calls
            or self._spent_tokens >= self._budgets.max_tokens
        )

    async def open(
        self,
        run_id: RunId,
        *,
        topic: str,
        branch: str,
        profile: str,
        tools: Literal["sandbox", "local"] = "sandbox",
        sandbox_network: Literal["none", "private"] = "none",
    ) -> None:
        """Start the run and seed the window with the role and the topic."""
        self._run_id = run_id
        await self._events.append(
            ev.RunStarted(
                branch=branch,
                profile=profile,
                model=self._model,
                tools=tools,
                sandbox_network=sandbox_network,
            ),
        )
        for server in self._catalog.servers():
            await self._events.append(
                ev.ToolsAttached(
                    server=server,
                    tools=self._catalog.tools_of(server),
                    roles=self._catalog.roles_of(server),
                ),
            )
        window = Window(
            self._tokenizer,
            self._policy,
            summarizer=self._summarizer,
            archive=self._archive,
            step=str(run_id),
        )
        await window.add_prompt(assemble(self._role.prompt_layers))
        await window.add_task(f"# Brainstorm\n\n{topic.strip()}")
        await window.add_notes(SESSION_NOTES)
        self._window = window

    async def say(self, text: str) -> Turn:
        """Run one exchange: the human's words, then the model's answer.

        Raises:
            SessionNotOpenError: if the session was never opened.
        """
        window = self._opened()
        number = len(self._turns) + 1
        await self._events.append(ev.BrainstormSaid(turn=number, text=text))
        await window.add_human(text)
        state = await self._step(f"turn-{number}", closing=False)
        return self._recorded(number, state)

    async def spec(self, origin: SpecOrigin) -> Spec | None:
        """Ask for the spec and bind the origin the harness knows.

        Args:
            origin: who ran this session, which the model is not asked for
                because it could only transcribe it (ADR 0026).

        Returns:
            The spec, or None when the model did not produce a valid draft.

        Raises:
            SessionNotOpenError: if the session was never opened.
        """
        window = self._opened()
        number = len(self._turns) + 1
        state = await self._step(f"spec-{number}", closing=True)
        turn = self._recorded(number, state)
        del window
        return turn.draft.for_origin(origin) if turn.draft is not None else None

    async def close(
        self, outcome: RunOutcome, reason: str, spec: Spec | None = None
    ) -> BrainstormResult:
        """Record the run's end and return what the session produced."""
        await self._events.append(ev.RunFinished(outcome=outcome, reason=reason))
        return BrainstormResult(
            run_id=self._run_id,
            outcome=outcome,
            turns=len(self._turns),
            spec=spec,
            tool_calls=self._spent_calls,
            tokens=self._spent_tokens,
            reason=reason,
        )

    def _opened(self) -> Window:
        """Return the session's window, or say it was never opened."""
        if self._window is None:
            msg = "the session was not opened; call open() first"
            raise SessionNotOpenError(msg)
        return self._window

    async def _step(self, suffix: str, *, closing: bool) -> LoopState:
        """Run one turn as a step against the shared window."""
        window = self._opened()
        step_id = StepId(derive_id(self._run_id, Stage.BRAINSTORM.value, suffix))
        config = LoopConfig(
            stage=Stage.BRAINSTORM.value,
            role=Role.BRAINSTORMER,
            model=self._model,
            allowlist=(
                frozenset()
                if closing
                else self._role.allowlist | self._catalog.allowlist_for(Role.BRAINSTORMER.value)
            ),
            catalog=self._catalog,
            budgets=self._remaining(closing=closing),
            thresholds=self._thresholds,
            limits=self._limits,
            temperature=self._role.temperature,
            # The closing turn is not a step that ran out of budget, so it is
            # not told to write its state into NOTES.md. It was always going
            # to be the artifact and the instruction says so.
            closing_instruction=CLOSING_INSTRUCTION if closing else "",
            # The conversation ends with no artifact demanded of it; only the
            # closing turn is asked for one, and `LoopConfig` derives its
            # schema, which is the one right value for it.
            final_artifact=ArtifactKind.SPEC_DRAFT if closing else None,
        )
        return await self._runner.run(config, window, step_id=step_id)

    def _remaining(self, *, closing: bool) -> Budgets:
        """Return what is left of the stage's budget, as one turn's budget.

        The closing turn is given a token allowance whatever is left, because
        a session that has spent its budget and cannot be asked for the spec
        has spent it on nothing.
        """
        calls = max(0, self._budgets.max_tool_calls - self._spent_calls)
        tokens = max(0, self._budgets.max_tokens - self._spent_tokens)
        if closing:
            return self._budgets.model_copy(
                update={"max_tool_calls": 0, "max_tokens": max(tokens, CLOSING_TOKENS)},
            )
        return self._budgets.model_copy(
            update={"max_tool_calls": calls, "max_tokens": tokens},
        )

    def _recorded(self, number: int, state: LoopState) -> Turn:
        """Fold one step's end state into a turn and charge the session."""
        self._spent_calls += state.budget.tool_calls_used
        self._spent_tokens += state.budget.tokens_used
        turn = Turn(
            number=number,
            said=_prose(state),
            tool_calls=state.budget.tool_calls_used,
            tokens=state.budget.tokens_used,
            stop_reason=state.stop_reason,
            draft=state.spec_draft,
        )
        self._turns.append(turn)
        return turn


CLOSING_TOKENS = 4_000
"""The floor on the closing turn's token allowance, whatever is left."""


class SessionNotOpenError(RuntimeError):
    """A session was used before it was opened."""


def _prose(state: LoopState) -> str:
    """Return what the model said for the human to read.

    A turn that ended on an artifact says so rather than showing JSON: the
    human reads the spec from the file, and a window's worth of braces on a
    terminal is not an answer to anything.
    """
    if state.spec_draft is not None:
        return f"wrote the spec: {state.spec_draft.title}"
    if state.final_text:
        return state.final_text
    if state.final_answer is not None:
        return state.final_answer.summary
    if state.stop_reason is not None:
        return f"the turn ended on {state.stop_reason.value} with nothing written"
    return ""

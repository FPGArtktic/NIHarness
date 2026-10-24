# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The planning stage: one spec in, one plan out.

This is entry B of the pipeline. A spec - written by a human, by an agent, or
by a brainstorm session - is turned into an ordered list of tasks by the
planner role, and the result waits for a human to approve it before any code
is written.

The stage is deliberately thin. It runs one step, binds what the model wrote
to the spec it was planned from, and lets the `Plan` schema decide whether the
result is a plan at all. There is no retry loop around a bad plan beyond the
one the tool loop already runs for an artifact that does not validate: a plan
the model cannot get right in a step is a spec problem, and sending it back to
the human with the reason costs less than three more attempts at the same
ambiguity.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from niharness.application.control import ControlDrain
from niharness.application.step_runner import StepRunner
from niharness.application.summarizer import Summarizer
from niharness.application.window import ContextArchive, Window
from niharness.domain import events as ev
from niharness.domain.artifacts import Decision, Plan, Spec, SpecVerdict
from niharness.domain.budgets import fit_to_host
from niharness.domain.context import CompactionPolicy
from niharness.domain.events import RunOutcome, StopReason
from niharness.domain.identity import RunId, StepId, derive_id
from niharness.domain.loop import LoopThresholds
from niharness.domain.pipeline import Stage, step_for
from niharness.domain.roles import DEFAULT_ROLES, SPEC_GATE_LAYERS, Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig, LoopState
from niharness.domain.tools import EMPTY_CATALOG, ToolCatalog, ToolLimits
from niharness.ports.control_channel import ControlChannel
from niharness.ports.event_sink import EventSink
from niharness.ports.tokenizer import Tokenizer
from niharness.prompts import assemble

PLAN_NOTES = (
    "Read the code before you plan it. You have read-only tools and no way to "
    "write; a task you cannot name the files for is a task you have not "
    "understood yet."
)
"""The priority-two reminder the planning step carries."""

_log = structlog.get_logger(__name__)


class PlanningResult(BaseModel):
    """What the planning stage produced, and what it cost."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    outcome: RunOutcome
    stop_reason: StopReason
    tool_calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    plan: Plan | None = None
    spec_verdict: SpecVerdict | None = Field(
        default=None,
        description="The spec gate's reviewer pass, when one ran.",
    )
    reason: str = ""

    @property
    def planned(self) -> bool:
        """True when there is a plan to approve."""
        return self.plan is not None


def render_spec(spec: Spec) -> str:
    """Render the spec as the planner reads it in its window.

    Not :func:`niharness.domain.markdown.render_spec`, which carries the
    artifact in a file and therefore repeats the whole thing as JSON for the
    gate. The planner needs the content once. What it does not need at all is
    the spec's digest: that is the harness's half of the artifact, bound on
    afterwards, and a digest in the window is sixty-four characters inviting a
    transcription error.
    """
    lines = [
        f"# Spec: {spec.title}",
        "",
        "## Goal",
        "",
        spec.goal,
        "",
    ]
    if spec.non_goals:
        lines += ["## Non-goals", "", *[f"- {item}" for item in spec.non_goals], ""]
    if spec.constraints:
        lines += ["## Constraints", "", *[f"- {item}" for item in spec.constraints], ""]
    lines += ["## Acceptance criteria", ""]
    lines += [
        f"- `{criterion.id}`: {criterion.statement} (verified by: {criterion.verification})"
        for criterion in spec.acceptance_criteria
    ]
    if spec.notes.strip():
        lines += ["", "## Notes", "", spec.notes.strip()]
    lines += [
        "",
        "Plan this spec. Every criterion above must be satisfied by at least one "
        "task, and every task must name the criteria it satisfies by those ids.",
    ]
    return "\n".join(lines)


class PlanningRun:
    """Runs the planning stage in a worktree: one step, one plan."""

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
        control: ControlChannel | None = None,
        catalog: ToolCatalog | None = None,
        generation_tps: float = 0.0,
    ) -> None:
        """Wire what the stage needs beyond the step executor.

        The arguments are the implementing run's minus the ones a read-only
        stage has no use for: there is no version control here because nothing
        is committed, and no revision limit because there is nothing to fix.

        Args:
            runner: the step executor, already holding the ports.
            events: the run's event sink.
            tokenizer: measures the window, through the backend.
            model: the model name requested for the planning step.
            policy: compaction policy; the shipped defaults when omitted.
            thresholds: loop detector thresholds.
            limits: tool argument and result caps.
            summarizer: the utility model, for compaction summaries.
            archive: where compaction puts what it drops.
            control: the inbox, so a planning run can be cancelled like any
                other.
            catalog: the external MCP tools this run reached.
            generation_tps: measured generation throughput, so the step's
                token budget is fitted to what its wall clock can pay for.
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
        self._control = ControlDrain(events, control)
        self._catalog = catalog or EMPTY_CATALOG
        self._generation_tps = generation_tps

    async def run(
        self,
        spec: Spec,
        run_id: RunId,
        *,
        branch: str,
        profile: str,
        tools: Literal["sandbox", "local"] = "sandbox",
        sandbox_network: Literal["none", "private"] = "none",
        review_spec: bool = True,
    ) -> PlanningResult:
        """Plan `spec`, and record what happened.

        Args:
            spec: the accepted spec. It arrives validated - the schema check
                is the first half of the spec gate and runs before a model is
                started, because a spec whose criteria are not testable is not
                worth a planning step.
            run_id: this run.
            branch: the worktree's branch.
            profile: the hardware profile in force.
            tools: where the tool server ran.
            sandbox_network: whether the sandbox had a network.
            review_spec: run the spec gate's reviewer pass before planning.
                On by default; what it costs is one no-tool step and what it
                buys is a spec that somebody read before work was planned
                from it.

        Returns:
            The plan, or the reason there is none.
        """
        await self._events.append(
            ev.RunStarted(
                branch=branch,
                profile=profile,
                model=self._model,
                spec_digest=spec.digest(),
                tools=tools,
                sandbox_network=sandbox_network,
            ),
        )
        reviewed: SpecVerdict | None = None
        if review_spec:
            reviewed = await self._review_spec(spec, run_id)
            if reviewed is None or reviewed.decision is Decision.NAK:
                return await self._refused(run_id, spec, reviewed)
        await self._events.append(
            ev.SpecAccepted(spec_digest=spec.digest(), reviewed=reviewed is not None),
        )
        for server in self._catalog.servers():
            await self._events.append(
                ev.ToolsAttached(
                    server=server,
                    tools=self._catalog.tools_of(server),
                    roles=self._catalog.roles_of(server),
                ),
            )

        state = await self._plan_step(spec, run_id)
        stop_reason = state.stop_reason or StopReason.ERROR

        def ended(
            outcome: RunOutcome, reason: str, plan: Plan | None = None
        ) -> Awaitable[PlanningResult]:
            """Finish with what the step actually spent, whatever the outcome."""
            return self._finish(
                run_id,
                outcome,
                reason,
                stop_reason,
                tool_calls=state.budget.tool_calls_used,
                tokens=state.budget.tokens_used,
                plan=plan,
                spec_verdict=reviewed,
            )

        cancelled = self._control.cancelled
        if cancelled is not None:
            return await ended(RunOutcome.CANCELLED, cancelled)

        draft = state.plan_draft
        if draft is None:
            return await ended(
                RunOutcome.FAILED,
                f"the planning step ended with no plan ({stop_reason.value})",
            )
        try:
            plan = draft.for_spec(spec.digest())
        except (ValidationError, ValueError) as error:
            # The draft validated on its own and still is not a plan: the task
            # list is not ordered, or an id repeats. That is a planning error
            # rather than a schema one, so it goes back to the human with what
            # the model wrote named in the reason.
            return await ended(RunOutcome.FAILED, f"the plan does not hold together: {error}")

        await self._events.append(
            ev.PlanCreated(
                plan_digest=plan.digest(),
                task_count=len(plan.tasks),
                imported=False,
            ),
        )
        _log.info(
            "plan.created",
            run_id=run_id,
            digest=str(plan.digest())[:12],
            tasks=len(plan.tasks),
        )
        return await ended(RunOutcome.COMPLETED, f"{len(plan.tasks)} task(s) planned", plan)

    async def _finish(
        self,
        run_id: RunId,
        outcome: RunOutcome,
        reason: str,
        stop_reason: StopReason,
        *,
        tool_calls: int,
        tokens: int,
        plan: Plan | None = None,
        spec_verdict: SpecVerdict | None = None,
    ) -> PlanningResult:
        """Record the run's terminal event and assemble the result."""
        await self._events.append(ev.RunFinished(outcome=outcome, reason=reason))
        _log.info("run.finished", outcome=outcome.value, reason=reason)
        return PlanningResult(
            run_id=run_id,
            outcome=outcome,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            tokens=tokens,
            plan=plan,
            spec_verdict=spec_verdict,
            reason=reason,
        )

    async def _review_spec(self, spec: Spec, run_id: RunId) -> SpecVerdict | None:
        """Run the spec gate's reviewer pass: one step, no tools, no code.

        Returns:
            The verdict, or None when the step produced none - which is itself
            a refusal to proceed, because planning from an unreviewed spec is
            what `--no-spec-review` is for and it has to be asked for.
        """
        definition = step_for(Stage.SPEC_GATE)
        step_id = StepId(derive_id(run_id, Stage.SPEC_GATE.value, str(spec.digest())))
        window = Window(
            self._tokenizer,
            self._policy,
            summarizer=self._summarizer,
            archive=self._archive,
            step=str(step_id),
        )
        await window.add_prompt(assemble(SPEC_GATE_LAYERS))
        await window.add_task(render_spec(spec))
        state = await self._runner.run(
            LoopConfig(
                stage=Stage.SPEC_GATE.value,
                role=Role.REVIEWER,
                model=self._model,
                allowlist=frozenset(),
                budgets=fit_to_host(definition.budgets, self._generation_tps),
                thresholds=self._thresholds,
                limits=self._limits,
                temperature=0.0,
                final_artifact=ArtifactKind.SPEC_VERDICT,
            ),
            window,
            step_id=step_id,
        )
        return state.spec_verdict

    async def _refused(
        self,
        run_id: RunId,
        spec: Spec,
        verdict: SpecVerdict | None,
    ) -> PlanningResult:
        """Finish without planning, saying what the spec is missing."""
        if verdict is None:
            reason = "the spec gate produced no verdict, so the spec was not reviewed"
        else:
            listed = "; ".join(verdict.missing)
            reason = f"the spec gate refused it: {verdict.summary} Missing: {listed}"
        _log.warning("spec.refused", run_id=run_id, digest=str(spec.digest())[:12])
        return await self._finish(
            run_id,
            RunOutcome.FAILED,
            reason,
            StopReason.FINAL_ANSWER if verdict else StopReason.ERROR,
            tool_calls=0,
            tokens=0,
            spec_verdict=verdict,
        )

    async def _plan_step(self, spec: Spec, run_id: RunId) -> LoopState:
        """Run the planner over the spec and return the loop's final state."""
        role = Role.PLANNER
        role_spec = DEFAULT_ROLES[role]
        definition = step_for(Stage.PLAN)
        step_id = StepId(derive_id(run_id, Stage.PLAN.value, str(spec.digest())))
        window = Window(
            self._tokenizer,
            self._policy,
            summarizer=self._summarizer,
            archive=self._archive,
            step=str(step_id),
        )
        await window.add_prompt(assemble(role_spec.prompt_layers))
        await window.add_task(render_spec(spec))
        await window.add_notes(PLAN_NOTES)

        config = LoopConfig(
            stage=Stage.PLAN.value,
            role=role,
            model=self._model,
            allowlist=role_spec.allowlist | self._catalog.allowlist_for(role.value),
            catalog=self._catalog,
            budgets=fit_to_host(definition.budgets, self._generation_tps),
            thresholds=self._thresholds,
            limits=self._limits,
            temperature=role_spec.temperature,
            # The schema is derived from the artifact by `LoopConfig` itself,
            # which is the one right value for it; naming it twice is how the
            # two drift apart.
            final_artifact=ArtifactKind.PLAN_DRAFT,
        )
        return await self._runner.run(
            config,
            window,
            step_id=step_id,
            should_stop=(
                (lambda: self._control.stop_requested(run_id))
                if self._control.listening
                else None
            ),
        )

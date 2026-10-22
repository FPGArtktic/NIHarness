# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The management server: the harness, driven by an agent.

The harness is an MCP client where tools are concerned. Here it is a *server*,
and the client is whatever supervises it - Claude, most likely. The point is
not convenience, it is cost: the local models read the files, write the
patches, run the tests and face the gate, and the supervisor pays only for
decisions. Every answer here is therefore a digest with a budget, never a log.

What this surface deliberately does not offer: reading project files, applying
a patch, committing, or running the gate. A supervisor with those would start
doing the work itself, which is the expense this exists to avoid - and the gate
would stop being the only way in.
"""

from __future__ import annotations

import json
from collections.abc import Coroutine
from typing import Any

import structlog
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict

from niharness.adapters.git import GitError, GitVcs
from niharness.config import HarnessConfig
from niharness.domain.artifacts import Answer
from niharness.domain.control import Driver
from niharness.domain.gate import GateTier, detect_languages, plan_gate
from niharness.domain.identity import Digest
from niharness.management import views
from niharness.management.plans import NoSuchPlanError, PlanStore
from niharness.management.runs import (
    NoSuchRunError,
    NotResumableError,
    RunRegistry,
    new_run_id,
)
from niharness.ports.vcs import Worktree

SERVER_NAME = "niharness"
"""How the management server identifies itself in the handshake."""

_log = structlog.get_logger(__name__)


class Outcome(BaseModel):
    """What every management tool returns: whether it worked, and a bounded digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    text: str


class Started(Outcome):
    """A run was started; the id is what every later call needs."""

    run_id: str = ""


class Stored(Outcome):
    """A plan was stored; the digest names it from here on."""

    plan_digest: str = ""


class Cursor(Outcome):
    """A page of events, and where to continue from."""

    cursor: int = 0


async def guarded(call: Coroutine[Any, Any, str]) -> Outcome:
    """Await `call` and turn a refusal into an answer the supervisor can read."""
    try:
        return Outcome(ok=True, text=await call)
    except (NoSuchRunError, NoSuchPlanError) as error:
        return Outcome(ok=False, text=f"no such thing: {error}")
    except (GitError, ValueError) as error:
        return Outcome(ok=False, text=str(error))


def build_server(config: HarnessConfig, *, name: str = SERVER_NAME) -> MCPServer:
    """Register the management tools for one project.

    Args:
        config: the project whose runs this server manages.
        name: the server's name in the handshake.

    Returns:
        A server ready to run over stdio.
    """
    server = MCPServer(name)
    registry = RunRegistry(config)
    plans = PlanStore(config)
    _register_reading(server, config, registry)
    _register_steering(server, config, registry, plans)
    _log.info("management.ready", name=name, project=str(config.project_root))
    return server


def _register_reading(server: MCPServer, config: HarnessConfig, registry: RunRegistry) -> None:
    """Register the tools that only look."""

    @server.tool()
    async def runs_list() -> Outcome:
        """List this project's runs: which are live, and what each one has done."""
        return await guarded(_runs_list(registry))

    @server.tool()
    async def run_status(run_id: str) -> Outcome:
        """What one run is doing, and what it has cost so far."""
        return await guarded(_run_status(registry, run_id))

    @server.tool()
    async def run_tasks(run_id: str) -> Outcome:
        """What happened to each task of the plan this run is executing."""
        return await guarded(_run_tasks(registry, run_id))

    @server.tool()
    async def run_events(
        run_id: str,
        since: int = 0,
        limit: int = views.DEFAULT_EVENT_LIMIT,
        kinds: str = "",
    ) -> Cursor:
        """Recent events of a run, as counts plus one line each.

        Pass the cursor back as `since` to see only what is new. `kinds` is an
        optional comma-separated filter, for example `tool.rejected,gate.finished`.
        """
        try:
            resolved = registry.require(run_id)
        except NoSuchRunError as error:
            return Cursor(ok=False, text=f"no run {error}", cursor=since)
        events = await registry.events(resolved)
        wanted = tuple(kind.strip() for kind in kinds.split(",") if kind.strip())
        text, cursor = views.render_events(events, since=since, limit=limit, kinds=wanted)
        return Cursor(ok=True, text=text, cursor=cursor)

    @server.tool()
    async def run_diff(run_id: str, revision: str = "HEAD") -> Outcome:
        """The diff a commit of this run introduced.

        This is the one call that returns source, and it returns only what the
        run itself produced - the change under review, not the project.
        """
        return await guarded(_run_diff(config, registry, run_id, revision))

    @server.tool()
    async def gate_plan(tier: str = "quick") -> Outcome:
        """The commands the gate would run in this project, for a tier."""
        return await guarded(_gate_plan(config, tier))

    @server.tool()
    async def config_show() -> Outcome:
        """The profile, models and budgets this project is configured with."""
        return await guarded(_config_show(config))


def _register_steering(
    server: MCPServer,
    config: HarnessConfig,
    registry: RunRegistry,
    plans: PlanStore,
) -> None:
    """Register the tools that change something."""

    @server.tool()
    async def plan_submit(markdown: str) -> Stored:
        """Validate a PLAN.md document and store it. Returns the digest that names it."""
        try:
            plan, path = plans.submit(markdown)
        except ValueError as error:
            return Stored(ok=False, text=f"the plan does not validate: {error}")
        return Stored(
            ok=True,
            text=f"{views.render_plan(plan)}\nstored at {path}",
            plan_digest=str(plan.digest()),
        )

    @server.tool()
    async def plan_approve(plan_digest: str) -> Outcome:
        """Approve a stored plan. `nih implement` runs the approved one."""
        return await guarded(_plan_approve(plans, plan_digest))

    @server.tool()
    async def implement_start(plan_digest: str = "", local_tools: bool = False) -> Started:
        """Start a run of the approved plan, in the background.

        Returns the run id immediately: the run outlives this call, and
        `run_status` is how you follow it.
        """
        try:
            plan, path = (
                plans.load(Digest(plan_digest))
                if plan_digest
                else (plans.approved(), config.approved_plan_path)
            )
        except NoSuchPlanError as error:
            return Started(ok=False, text=f"no stored plan {error}")
        if plan is None:
            return Started(ok=False, text="no plan is approved; call plan_approve first")
        run_id = new_run_id(plan)
        registry.start_detached(run_id, plan, plan_file=path, local_tools=local_tools)
        where = "on this host" if local_tools else "in the sandbox"
        return Started(
            ok=True,
            text=f"run {run_id} started {where}, {len(plan.tasks)} task(s)",
            run_id=run_id,
        )

    @server.tool()
    async def run_resume(run_id: str, local_tools: bool = False) -> Started:
        """Continue a run that was cancelled or stopped short, in the background.

        The tasks it already finished are skipped; the rest run in the same
        worktree and append to the same event log.
        """
        try:
            point = await registry.resume_point(registry.require(run_id))
            plan, path = plans.load(Digest(point.plan_digest))
        except (NoSuchRunError, NotResumableError) as error:
            return Started(ok=False, text=str(error))
        except NoSuchPlanError as error:
            return Started(
                ok=False,
                text=f"the plan {error} this run was executing is not stored; submit it again",
            )
        registry.start_detached(
            point.run_id,
            plan,
            plan_file=path,
            local_tools=local_tools,
            resume=True,
        )
        return Started(
            ok=True,
            text=(
                f"run {point.run_id} resumed from event {point.from_seq}, "
                f"skipping {len(point.done)} finished task(s)"
            ),
            run_id=point.run_id,
        )

    @server.tool()
    async def run_cancel(run_id: str, reason: str = "") -> Outcome:
        """Ask a run to stop at its next step boundary, leaving a resumable snapshot."""
        return await guarded(_run_cancel(registry, run_id, reason))

    @server.tool()
    async def questions_answer(run_id: str, answers_json: str) -> Outcome:
        """Answer the clarifying questions a run is waiting on.

        `answers_json` is a JSON array of objects with `question_id` and either
        `option_id` or `free_text`.
        """
        return await guarded(_questions_answer(registry, run_id, answers_json))


# The tool bodies live outside the closures so they can be tested directly.


async def _runs_list(registry: RunRegistry) -> str:
    """Render the registry."""
    return views.render_runs(await registry.collect())


async def _run_status(registry: RunRegistry, run_id: str) -> str:
    """Render one run's state."""
    resolved = registry.require(run_id)
    return views.render_status(
        await registry.entry(resolved), await registry.snapshot(resolved)
    )


async def _run_tasks(registry: RunRegistry, run_id: str) -> str:
    """Render per-task progress."""
    resolved = registry.require(run_id)
    return views.render_tasks(await registry.snapshot(resolved))


async def _run_diff(
    config: HarnessConfig,
    registry: RunRegistry,
    run_id: str,
    revision: str,
) -> str:
    """Render a commit of the run's worktree."""
    resolved = registry.require(run_id)
    path = registry.worktree_path(resolved)
    if not path.is_dir():
        return f"the worktree of {resolved} is gone; its commits are on harness/run-{resolved}"
    vcs = GitVcs(config.project_root, config.worktrees_dir)
    worktree = Worktree(path=str(path), branch=f"harness/run-{resolved}", base_sha="")
    return views.bounded(await vcs.diff(worktree, revision), views.DIFF_BUDGET)


async def _gate_plan(config: HarnessConfig, tier: str) -> str:
    """Render the gate plan for this project."""
    vcs = GitVcs(config.project_root)
    worktree = Worktree(path=str(config.project_root), branch="", base_sha="")
    languages = detect_languages(await vcs.list_files(worktree))
    commands = plan_gate(GateTier(tier), languages)
    lines = [
        f"gate {tier}: {', '.join(sorted(item.value for item in languages)) or 'no languages'}",
        *[f"  [{command.kind.value:6}] {command.name}" for command in commands],
    ]
    return views.bounded("\n".join(lines))


async def _config_show(config: HarnessConfig) -> str:
    """Render the parts of the configuration a supervisor acts on."""
    models = config.models
    lines = [
        f"profile: {config.profile}",
        f"main model: {models.main.name} ({models.main.quantization}, "
        f"{models.main.context_window} ctx) at {models.main.base_url}",
        f"utility model: {models.utility.name} at {models.utility.base_url}",
        f"compaction: threshold {config.compact.threshold}, "
        f"window {config.compact.context_window}",
        f"fix loop limit: {config.max_revisions}",
        f"sandbox: {config.sandbox.image}, network {config.sandbox.network}, "
        f"{config.sandbox.cpus} cpus, {config.sandbox.memory}",
    ]
    return views.bounded("\n".join(lines))


async def _plan_approve(plans: PlanStore, plan_digest: str) -> str:
    """Approve a stored plan."""
    target = plans.approve(Digest(plan_digest))
    return f"plan {plan_digest} approved; it is now {target}"


async def _run_cancel(registry: RunRegistry, run_id: str, reason: str) -> str:
    """Submit a cancel request."""
    resolved = registry.require(run_id)
    await registry.cancel(resolved, reason, Driver.MCP)
    return f"cancel requested for {resolved}; it stops at the next step boundary"


async def _questions_answer(registry: RunRegistry, run_id: str, answers_json: str) -> str:
    """Submit answers to a waiting run."""
    resolved = registry.require(run_id)
    raw = json.loads(answers_json)
    if not isinstance(raw, list) or not raw:
        msg = "answers_json must be a non-empty JSON array"
        raise ValueError(msg)
    answers = tuple(Answer.model_validate(entry) for entry in raw)
    await registry.answer(resolved, answers, Driver.MCP)
    return f"{len(answers)} answer(s) submitted to {resolved}"


def serve(config: HarnessConfig) -> None:
    """Run the management server over stdio until the client disconnects."""
    build_server(config).run("stdio")

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih implement`: execute an approved plan.

This is where the adapters meet the domain. Everything it does is wiring: pick
the plan, make a worktree, open the event log, start the tool server, hand the
pieces to the application layer, and report what happened.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Literal

import httpx
import typer
from mcp import StdioServerParameters
from pydantic import ValidationError

from niharness.adapters.git import GitError, GitVcs
from niharness.adapters.jsonl_control import JsonlControlChannel
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.mcp import ExternalToolsError, connect_tools
from niharness.adapters.openai_compat import AirGapViolationError, OpenAiCompatBackend
from niharness.adapters.podman import (
    PodmanSandbox,
    SandboxImageMissingError,
    SandboxUnavailableError,
)
from niharness.application import ImplementationRun, RunResult, StepRunner
from niharness.application.summarizer import ModelSummarizer
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.cli.tools import external_servers
from niharness.config import HarnessConfig, ModelConfig
from niharness.domain.artifacts import Plan
from niharness.domain.identity import Digest, RunId
from niharness.domain.markdown import ArtifactNotFoundError, parse_plan
from niharness.management.plans import NoSuchPlanError, PlanStore
from niharness.management.runs import (
    NoSuchRunError,
    NotResumableError,
    ResumePoint,
    RunRegistry,
    new_run_id,
    pid_file,
)
from niharness.ports.vcs import Worktree

EX_UNAVAILABLE = 69
"""`sysexits.h`: a service the command needs is not there."""


def load_plan(config: HarnessConfig, explicit: Path | None) -> Plan:
    """Return the plan to execute.

    Raises:
        typer.Exit: if there is no plan, or it does not validate.
    """
    source = explicit or config.approved_plan_path
    if not source.is_file():
        typer.secho(f"no plan at {source}", fg=typer.colors.RED, err=True)
        typer.echo("  write one, or approve one: nih plan SPEC.md", err=True)
        raise typer.Exit(1)
    try:
        return parse_plan(source.read_text(encoding="utf-8"))
    except (ArtifactNotFoundError, ValidationError) as error:
        typer.secho(f"{source}: invalid plan", fg=typer.colors.RED, err=True)
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error


def server_parameters(
    config: HarnessConfig,
    worktree: Worktree,
    *,
    local: bool,
) -> StdioServerParameters:
    """Return how to start the tool server: in the sandbox, or on this host."""
    if local:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "niharness.toolserver", "--root", worktree.path],
        )
    sandbox = PodmanSandbox(
        config.sandbox.image,
        Path(worktree.path),
        cpus=config.sandbox.cpus,
        memory=config.sandbox.memory,
        pids_limit=config.sandbox.pids_limit,
        binary=config.sandbox.binary,
    )
    argv = sandbox.tool_server_argv()
    return StdioServerParameters(command=argv[0], args=list(argv[1:]))


async def _check_sandbox(config: HarnessConfig, worktree: Worktree) -> None:
    """Fail early and clearly when Podman or the image is not there.

    Raises:
        typer.Exit: when the sandbox cannot be used.
    """
    sandbox = PodmanSandbox(
        config.sandbox.image,
        Path(worktree.path),
        binary=config.sandbox.binary,
    )
    try:
        await sandbox.ensure_ready()
    except (SandboxUnavailableError, SandboxImageMissingError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        typer.echo(
            "  or run the tools on this host instead, outside the container:\n"
            "    nih implement --unsafe-local-tools",
            err=True,
        )
        raise typer.Exit(EX_UNAVAILABLE) from error


def _backend(model: ModelConfig) -> OpenAiCompatBackend:
    """Build a client for one served model."""
    return OpenAiCompatBackend(
        model.base_url,
        model.name,
        context_window=model.context_window,
        supports_grammar=model.supports_grammar,
        supports_reasoning=model.supports_reasoning,
        reasoning_effort=model.reasoning_effort,
    )


async def execute(
    config: HarnessConfig,
    plan: Plan,
    *,
    local_tools: bool,
    run_id: RunId | None = None,
    quiet: bool = False,
    resume: ResumePoint | None = None,
) -> RunResult:
    """Create the worktree, start the tools, and run the plan.

    Raises:
        typer.Exit: when the worktree cannot be created or the sandbox is absent.
    """
    vcs = GitVcs(config.project_root, config.worktrees_dir)
    run_id = resume.run_id if resume else (run_id or new_run_id(plan))
    try:
        worktree = (
            await vcs.open_worktree(run_id) if resume else await vcs.create_worktree(run_id)
        )
    except GitError as error:
        verb = "open" if resume else "create"
        typer.secho(f"cannot {verb} the run worktree: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error

    if not local_tools:
        await _check_sandbox(config, worktree)

    sink = JsonlEventSink(config.runs_dir, run_id)
    control = JsonlControlChannel(config.runs_dir, run_id)
    main = _backend(config.models.main)
    utility = _backend(config.models.utility)
    mode: Literal["sandbox", "local"] = "local" if local_tools else "sandbox"

    if not quiet:
        typer.echo(f"run {run_id}")
        typer.echo(f"  worktree: {worktree.path} ({worktree.branch})")
        typer.echo(f"  model: {config.models.main.name} at {config.models.main.base_url}")
        typer.echo(f"  tools: {mode}")
        for server in config.mcp.servers:
            granted = ", ".join(role.value for role in server.roles)
            typer.echo(f"  mcp: {server.name} -> {', '.join(server.tools)} ({granted})")
        remaining = len(plan.tasks) - len(resume.done) if resume else len(plan.tasks)
        typer.echo(f"  tasks: {remaining} of {len(plan.tasks)}")

    async with AsyncExitStack() as stack:
        stack.push_async_callback(main.aclose)
        stack.push_async_callback(utility.aclose)
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                external_servers(config),
                limits=config.tools,
            ),
        )
        runner = StepRunner(
            backend=main,
            tools=tools,
            events=sink,
            vcs=vcs,
            worktree=worktree,
        )
        run = ImplementationRun(
            runner=runner,
            events=sink,
            tokenizer=main,
            model=config.models.main.name,
            policy=config.compact,
            thresholds=config.loop,
            limits=config.tools,
            summarizer=ModelSummarizer(utility, config.models.utility.name),
            vcs=vcs,
            worktree=worktree,
            control=control,
            catalog=tools.catalog,
            max_revisions=config.max_revisions,
        )
        with pid_file(config.runs_dir, run_id):
            return await run.run(
                plan,
                run_id,
                branch=worktree.branch,
                profile=config.profile,
                tools=mode,
                done=resume.done if resume else (),
                resumed_from=resume.from_seq if resume else 0,
            )
    raise AssertionError("unreachable: the context manager always returns")  # pragma: no cover


def report(result: RunResult, runs_dir: Path) -> None:
    """Print what the run did, and where to read the rest."""
    typer.echo("")
    pending = False
    for task in result.tasks:
        mark = "ok  " if task.finished else "FAIL"
        typer.echo(
            f"  [{mark}] {task.task_id}: {len(task.steps)} step(s), "
            f"{task.revisions} revision(s), {task.tool_calls} tool call(s), "
            f"{task.tokens} tokens",
        )
        for sha in task.commits:
            typer.echo(f"         commit {sha[:12]}")
        if not task.finished:
            typer.echo(f"         {task.reason}")
        pending = pending or task.full_gate_pending
    typer.echo("")
    if pending:
        typer.secho(
            "the full gate tier has not run: uv run nih gate --tier full",
            fg=typer.colors.YELLOW,
        )
    if result.completed:
        typer.secho(f"run {result.run_id}: all tasks done", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"run {result.run_id}: stopped at {result.stopped_at}",
            fg=typer.colors.RED,
            err=True,
        )
    typer.echo(f"  events: {runs_dir / result.run_id / 'events.jsonl'}")
    typer.echo(f"  status: nih status {result.run_id}")


def implement(
    config: HarnessConfig,
    plan_file: Path | None,
    *,
    local_tools: bool,
    detached: bool = False,
    run_id: str | None = None,
    json_mode: bool = False,
) -> None:
    """Run `nih implement` end to end.

    Raises:
        typer.Exit: on a failed run, or when a backend cannot be reached.
    """
    plan = load_plan(config, plan_file)
    if detached:
        started = RunRegistry(config).start_detached(
            new_run_id(plan),
            plan,
            plan_file=plan_file,
            local_tools=local_tools,
        )
        emit(
            {"ok": True, "run_id": started, "detached": True, "tasks": len(plan.tasks)},
            json_mode=json_mode,
            human=(
                f"run {started} started in the background\n"
                f"  status: nih status {started}\n"
                f"  cancel: nih cancel {started}"
            ),
        )
        return
    if config.mcp.servers:
        typer.secho(
            f"{len(config.mcp.servers)} declared MCP server(s) will be started as "
            "configured: their tools run wherever their command runs, which is outside "
            "the sandbox unless the command is a container. The gate is unaffected - "
            "they cannot commit.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    if local_tools:
        typer.secho(
            "running tools on this host, outside the sandbox: they can reach the "
            "network and whatever this user can read. The worktree is still the "
            "only path the server will write to, and the gate still runs.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    try:
        result = blocking(
            execute(
                config,
                plan,
                local_tools=local_tools,
                run_id=RunId(run_id) if run_id else None,
                quiet=json_mode,
            ),
        )
    except AirGapViolationError as error:
        fail(str(error), json_mode=json_mode)
        raise typer.Exit(1) from error
    except ExternalToolsError as error:
        fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
        raise typer.Exit(EX_UNAVAILABLE) from error
    except httpx.HTTPError as error:
        fail(
            f"the model backend at {config.models.main.base_url} did not answer: {error}",
            json_mode=json_mode,
            code=EX_UNAVAILABLE,
        )
        raise typer.Exit(EX_UNAVAILABLE) from error
    if json_mode:
        emit(
            {"ok": result.completed, "run": result.model_dump(mode="json")},
            json_mode=True,
        )
    else:
        report(result, config.runs_dir)
    if not result.completed:
        raise typer.Exit(1)


def resume(config: HarnessConfig, run_id: str, *, local_tools: bool, json_mode: bool) -> None:
    """Continue a run that was cancelled or stopped short.

    The tasks it already finished are skipped - their commits are on the branch
    already - and the rest run in the same worktree, appending to the same
    event log.

    Raises:
        typer.Exit: when the run cannot be continued, or the run fails.
    """
    registry = RunRegistry(config)
    try:
        resolved = registry.require(run_id)
        point = asyncio.run(registry.resume_point(resolved))
    except (NoSuchRunError, NotResumableError) as error:
        fail(str(error), {"run_id": run_id}, json_mode=json_mode)
        raise typer.Exit(1) from error
    try:
        plan, _ = PlanStore(config).load(Digest(point.plan_digest))
    except NoSuchPlanError as error:
        fail(
            f"the plan {point.plan_digest} this run was executing is not stored; "
            "submit it again before resuming",
            {"run_id": run_id, "plan_digest": point.plan_digest},
            json_mode=json_mode,
        )
        raise typer.Exit(1) from error

    if not json_mode:
        typer.echo(
            f"resuming {resolved} from event {point.from_seq}, "
            f"skipping {len(point.done)} finished task(s)",
        )
    try:
        result = blocking(
            execute(config, plan, local_tools=local_tools, quiet=json_mode, resume=point),
        )
    except ExternalToolsError as error:
        fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
        raise typer.Exit(EX_UNAVAILABLE) from error
    except httpx.HTTPError as error:
        fail(
            f"the model backend at {config.models.main.base_url} did not answer: {error}",
            json_mode=json_mode,
            code=EX_UNAVAILABLE,
        )
        raise typer.Exit(EX_UNAVAILABLE) from error
    if json_mode:
        emit({"ok": result.completed, "run": result.model_dump(mode="json")}, json_mode=True)
    else:
        report(result, config.runs_dir)
    if not result.completed:
        raise typer.Exit(1)

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih brainstorm`: entry A, a conversation at a terminal that writes SPEC.md.

§9 describes this in the TUI, with the running draft in a pane. The TUI reads
the event log and writes the control inbox; it does not type for the human, so
the session has to exist as a command first and the pane is a view of it. This
is that command.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

import typer

from niharness.adapters.git import GitError, GitVcs
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.mcp import connect_tools
from niharness.application.brainstorm import BrainstormResult, BrainstormSession
from niharness.application.step_runner import StepRunner
from niharness.application.summarizer import ModelSummarizer
from niharness.application.window import ContextArchive
from niharness.cli.retrieval import open_retrieval
from niharness.cli.tools import external_servers
from niharness.cli.wiring import (
    backend_for,
    check_sandbox,
    network_note,
    server_parameters,
)
from niharness.config import ModelConfig
from niharness.domain.artifacts import SpecOrigin
from niharness.domain.events import RunOutcome
from niharness.domain.markdown import render_spec
from niharness.management.runs import pid_file, seeded_run_id

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from niharness.config import HarnessConfig

SPEC_COMMAND = "/spec"
PLAN_COMMAND = "/plan"
QUIT_COMMAND = "/quit"

HELP = f"""  {SPEC_COMMAND}   write the spec and end the session
  {PLAN_COMMAND}   write the spec, then say how to plan it
  {QUIT_COMMAND}   end with nothing written
anything else is said to the model."""
"""What the session accepts, printed once at the start."""


def execute(
    config: HarnessConfig,
    *,
    topic: str,
    local_tools: bool,
    out: Path,
    reader: TextIO | None = None,
    quiet: bool = False,
) -> BrainstormResult:
    """Run the session to its end and write the spec if there is one.

    Synchronous, because a conversation is driven by a human at a keyboard and
    the loop is "read a line, run a step". The step executor is async and
    `blocking` bridges the two, exactly as every other command does.
    """
    from niharness.cli.blocking import blocking

    return blocking(
        _session(
            config,
            topic=topic,
            local_tools=local_tools,
            out=out,
            reader=reader or sys.stdin,
            quiet=quiet,
        ),
    )


async def _session(
    config: HarnessConfig,
    *,
    topic: str,
    local_tools: bool,
    out: Path,
    reader: TextIO,
    quiet: bool,
) -> BrainstormResult:
    """Make a worktree, start the tools, and talk until the human stops.

    The brainstormer is read-only and still gets a worktree of its own, for
    the reason the planner does: the tool server confines every path to one
    root, and the user's checkout is not it.

    Raises:
        typer.Exit: when the worktree cannot be created or the sandbox is absent.
    """
    vcs = GitVcs(config.project_root, config.worktrees_dir)
    run_id = seeded_run_id(topic)
    try:
        worktree = await vcs.create_worktree(run_id)
    except GitError as error:
        typer.secho(f"cannot create the run worktree: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error

    if not local_tools:
        await check_sandbox(config, worktree, command="brainstorm")

    sink = JsonlEventSink(config.runs_dir, run_id)
    main = backend_for(_thinking(config))
    utility = backend_for(config.models.utility)
    mode: Literal["sandbox", "local"] = "local" if local_tools else "sandbox"

    if not quiet:
        typer.echo(f"run {run_id}")
        typer.echo(f"  worktree: {worktree.path} ({worktree.branch})")
        typer.echo(f"  model: {_thinking(config).name} at {_thinking(config).base_url}")
        typer.echo(f"  tools: {mode}{network_note(config, local_tools)} (read-only)")
        typer.echo(HELP)
        typer.echo("")

    async with AsyncExitStack() as stack:
        stack.push_async_callback(main.aclose)
        stack.push_async_callback(utility.aclose)
        retrieval = await stack.enter_async_context(open_retrieval(config))
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                external_servers(config),
                limits=config.tools,
                local=retrieval.tools(config, run_id=run_id),
            ),
        )
        session = BrainstormSession(
            runner=StepRunner(backend=main, tools=tools, events=sink),
            events=sink,
            tokenizer=main,
            model=_thinking(config).name,
            policy=config.compact,
            thresholds=config.loop,
            limits=config.tools,
            summarizer=ModelSummarizer(utility, config.models.utility.name),
            archive=(
                ContextArchive(
                    store=retrieval.store,
                    collection=config.vector.run_collection(run_id),
                    run_id=run_id,
                )
                if retrieval.store is not None
                else None
            ),
            catalog=tools.catalog,
            generation_tps=_thinking(config).measured_generation_tps,
        )
        with pid_file(config.runs_dir, run_id):
            await session.open(
                run_id,
                topic=topic,
                branch=worktree.branch,
                profile=config.profile,
                tools=mode,
                sandbox_network=config.sandbox.network,
            )
            return await _talk(session, out=out, reader=reader, quiet=quiet)
    raise AssertionError("unreachable: the context manager always returns")  # pragma: no cover


async def _talk(
    session: BrainstormSession,
    *,
    out: Path,
    reader: TextIO,
    quiet: bool,
) -> BrainstormResult:
    """Read a line, run a turn, until a command or the end of the input."""
    while True:
        line = _asked(reader, quiet=quiet)
        if line is None or line == QUIT_COMMAND:
            return await session.close(RunOutcome.CANCELLED, "the human ended the session")
        if line in {SPEC_COMMAND, PLAN_COMMAND}:
            return await _write(session, out=out, then_plan=line == PLAN_COMMAND, quiet=quiet)
        if not line:
            continue
        turn = await session.say(line)
        if not quiet:
            typer.echo(f"\n{turn.said}\n")
        if session.exhausted and not quiet:
            typer.secho(
                f"the stage's budget is spent ({turn.number} turns). "
                f"{SPEC_COMMAND} still works; nothing else does.",
                fg=typer.colors.YELLOW,
                err=True,
            )


async def _write(
    session: BrainstormSession,
    *,
    out: Path,
    then_plan: bool,
    quiet: bool,
) -> BrainstormResult:
    """Ask for the spec, write it, and say what happens next."""
    spec = await session.spec(SpecOrigin.BRAINSTORM)
    if spec is None:
        typer.secho(
            "the session produced no valid spec; nothing was written",
            fg=typer.colors.RED,
            err=True,
        )
        return await session.close(RunOutcome.FAILED, "no spec")
    out.write_text(render_spec(spec), encoding="utf-8")
    if not quiet:
        typer.secho(f"wrote {out}", fg=typer.colors.GREEN)
        typer.echo(f"  {spec.title}: {len(spec.acceptance_criteria)} acceptance criteria")
        # Not run from here. A stage boundary starts a fresh run seeded only by
        # the artifact (§4), and the artifact is now on disk - so the next
        # stage is a command the human runs, which is also what makes the spec
        # reviewable before anything is planned from it.
        lead = "next" if then_plan else "plan it with"
        typer.echo(f"  {lead}: nih plan {out}")
    return await session.close(RunOutcome.COMPLETED, f"wrote {out}", spec=spec)


def _asked(reader: TextIO, *, quiet: bool) -> str | None:
    """Return the next line, or None at the end of the input."""
    if not quiet:
        typer.echo("> ", nl=False)
    line = reader.readline()
    if not line:
        return None
    return line.strip()


def _thinking(config: HarnessConfig) -> ModelConfig:
    """Return the model this session runs on.

    §7 gives brainstorming the Thinking variant, at a higher temperature, and
    allows the swap because a stage boundary is the one place a swap is
    allowed. A host that serves no Thinking model gets the main one, which is
    a worse brainstormer and a working session.
    """
    return config.models.thinking or config.models.main

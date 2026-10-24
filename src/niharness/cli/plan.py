# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih plan`: the planning stage, the plan gate, and approval.

Entry B of the pipeline lives here: a spec goes in, an ordered plan comes out,
and the plan waits for a human. The waiting is the point - `nih implement`
reads `.nih/approved-plan.md`, so nothing a planner wrote reaches the code
until someone approves it by name.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from pydantic import ValidationError
from typer.core import TyperGroup

from niharness.adapters.git import GitError, GitVcs
from niharness.adapters.jsonl_control import JsonlControlChannel
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.local_http import AirGapViolationError, explain
from niharness.adapters.mcp import ExternalToolsError, connect_tools
from niharness.application import ContextArchive, PlanningResult, PlanningRun, StepRunner
from niharness.application.summarizer import ModelSummarizer
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.cli.retrieval import open_retrieval
from niharness.cli.tools import external_servers
from niharness.cli.unavailable import unavailable
from niharness.cli.wiring import (
    backend_for,
    check_sandbox,
    network_note,
    retrieval_line,
    server_parameters,
)
from niharness.config import HarnessConfig
from niharness.domain.artifacts import Spec
from niharness.domain.identity import Digest
from niharness.domain.markdown import (
    ArtifactNotFoundError,
    parse_plan,
    parse_spec,
    render_plan,
)
from niharness.management.plans import NoSuchPlanError, PlanStore
from niharness.management.runs import new_run_id, pid_file

EX_UNAVAILABLE = 69
"""`sysexits.h`: a service the command needs is not there."""

TIMEOUT_HINT = "On a slow host raise [models.main] request_timeout_seconds."
"""What to suggest when the model stops answering mid-generation."""

ProjectRoot = Annotated[
    Path,
    typer.Option("--project-root", "-C", help="The project to operate on.", show_default="."),
]

JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print one JSON object instead of the human form."),
]

DEFAULT_COMMAND = "spec"
"""`nih plan SPEC.md` is a shorthand for `nih plan spec SPEC.md`."""


class PlanGroup(TyperGroup):
    """A group whose first positional argument may be a path instead of a subcommand.

    `nih plan SPEC.md` has to keep working next to `nih plan check` and
    `nih plan import`, so an argument that is not a known subcommand is routed
    to the default one rather than reported as a usage error.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:  # noqa: ANN401 - click Context
        """Insert the default subcommand when the first argument is a path."""
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [DEFAULT_COMMAND, *args]
        parsed: list[str] = super().parse_args(ctx, args)
        return parsed


app = typer.Typer(
    cls=PlanGroup,
    help="Plan a spec, validate a plan, or import one written elsewhere.",
    no_args_is_help=True,
)


def load_spec(path: Path, *, json_mode: bool) -> Spec:
    """Read and validate a spec: the first half of the spec gate, offline.

    No model is started for a spec that cannot pass the schema, and the schema
    is not a formality - every acceptance criterion has to say how it is
    verified, and a verification that only restates an intention is rejected.
    The reviewer half of the gate does not exist yet; see `docs/roadmap.md`.

    Raises:
        typer.Exit: when the file cannot be read or does not carry a spec.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"cannot read {path}: {error}", json_mode=json_mode)
        raise typer.Exit(1) from error
    try:
        return parse_spec(text)
    except (ArtifactNotFoundError, ValidationError) as error:
        fail(f"{path}: invalid spec", {"detail": str(error)}, json_mode=json_mode)
        raise typer.Exit(1) from error


async def execute(
    config: HarnessConfig,
    spec: Spec,
    *,
    local_tools: bool,
    review_spec: bool = True,
    quiet: bool = False,
) -> PlanningResult:
    """Make a worktree, start the tools, and run the planning step.

    The planner is read-only, and it still gets a worktree of its own. That is
    not ceremony: the tool server confines every path to a root, and the root
    it is given is the one directory a stage may see. Pointing it at the
    user's checkout would make a read-only stage the one place where that
    confinement does not apply.

    Raises:
        typer.Exit: when the worktree cannot be created or the sandbox is absent.
    """
    vcs = GitVcs(config.project_root, config.worktrees_dir)
    run_id = new_run_id(spec)
    try:
        worktree = await vcs.create_worktree(run_id)
    except GitError as error:
        typer.secho(f"cannot create the run worktree: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error

    if not local_tools:
        await check_sandbox(config, worktree, command="plan")

    sink = JsonlEventSink(config.runs_dir, run_id)
    control = JsonlControlChannel(config.runs_dir, run_id)
    main = backend_for(config.models.main)
    utility = backend_for(config.models.utility)
    mode = "local" if local_tools else "sandbox"

    if not quiet:
        typer.echo(f"run {run_id}")
        typer.echo(f"  worktree: {worktree.path} ({worktree.branch})")
        typer.echo(f"  model: {config.models.main.name} at {config.models.main.base_url}")
        typer.echo(f"  tools: {mode}{network_note(config, local_tools)}")
        typer.echo(f"  spec: {spec.title} ({len(spec.acceptance_criteria)} criteria)")

    async with AsyncExitStack() as stack:
        stack.push_async_callback(main.aclose)
        stack.push_async_callback(utility.aclose)
        retrieval = await stack.enter_async_context(open_retrieval(config))
        if not quiet:
            typer.echo(f"  retrieval: {retrieval_line(config, retrieval)}")
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                external_servers(config),
                limits=config.tools,
                local=retrieval.tools(config, run_id=run_id),
            ),
        )
        runner = StepRunner(backend=main, tools=tools, events=sink)
        run = PlanningRun(
            runner=runner,
            events=sink,
            tokenizer=main,
            model=config.models.main.name,
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
            control=control,
            catalog=tools.catalog,
            generation_tps=config.models.main.measured_generation_tps,
        )
        with pid_file(config.runs_dir, run_id):
            return await run.run(
                spec,
                run_id,
                branch=worktree.branch,
                profile=config.profile,
                tools="local" if local_tools else "sandbox",
                sandbox_network=config.sandbox.network,
                review_spec=review_spec,
            )
    raise AssertionError("unreachable: the context manager always returns")  # pragma: no cover


@app.command(DEFAULT_COMMAND)
def plan_spec(
    spec_file: Annotated[Path, typer.Argument(metavar="SPEC.md", help="Path to SPEC.md")],
    root: ProjectRoot = Path(),
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the plan.")
    ] = Path("PLAN.md"),
    local_tools: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-tools",
            help="Run the tool server on this host instead of in the sandbox.",
        ),
    ] = False,
    approve: Annotated[
        bool,
        typer.Option("--approve", help="Approve the plan at once, skipping the human stop."),
    ] = False,
    review_spec: Annotated[
        bool,
        typer.Option(
            "--spec-review/--no-spec-review",
            help="Run the spec gate's reviewer pass before planning.",
        ),
    ] = True,
    json_mode: JsonOption = False,
) -> None:
    """Run the planning stage on a spec, producing PLAN.md for approval."""
    config = HarnessConfig.load(root.resolve(), profile=profile)
    spec = load_spec(spec_file, json_mode=json_mode)
    try:
        result = blocking(
            execute(
                config,
                spec,
                local_tools=local_tools,
                review_spec=review_spec,
                quiet=json_mode,
            ),
        )
    except AirGapViolationError as error:
        fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
        raise typer.Exit(EX_UNAVAILABLE) from error
    except ExternalToolsError as error:
        fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
        raise typer.Exit(EX_UNAVAILABLE) from error
    except httpx.HTTPError as error:
        fail(
            f"the model backend did not answer: {explain(error, timeout_hint=TIMEOUT_HINT)}",
            json_mode=json_mode,
            code=EX_UNAVAILABLE,
        )
        raise typer.Exit(EX_UNAVAILABLE) from error

    if result.plan is None:
        refused = result.spec_verdict
        fail(
            f"no plan: {result.reason}",
            {
                "run_id": str(result.run_id),
                "outcome": result.outcome.value,
                "stop_reason": result.stop_reason.value,
                "tool_calls": result.tool_calls,
                "tokens": result.tokens,
                **(
                    {"spec_missing": list(refused.missing), "spec_summary": refused.summary}
                    if refused is not None
                    else {}
                ),
            },
            json_mode=json_mode,
        )
        raise typer.Exit(1)

    plan = result.plan
    output.write_text(render_plan(plan), encoding="utf-8")
    store = PlanStore(config)
    store.submit(render_plan(plan))
    approved = store.approve(plan.digest()) if approve else None

    human = [
        "",
        f"{output}: {len(plan.tasks)} task(s) planned from {spec_file}",
        f"  plan digest: {plan.digest()}",
        f"  cost: {result.tool_calls} tool call(s), {result.tokens} token(s)",
    ]
    human += [f"  {index}. {task.id}: {task.title}" for index, task in enumerate(plan.tasks, 1)]
    if plan.open_questions:
        human += ["", "  open questions the planner had to assume past:"]
        human += [f"    - {question}" for question in plan.open_questions]
    if approved is not None:
        human.append(f"\n  approved: {approved}")
        human.append("  next: nih implement")
    else:
        human.append(f"\n  read it, then: nih plan approve {output}")
    emit(
        {
            "ok": True,
            "run_id": str(result.run_id),
            "plan": str(output),
            "plan_digest": str(plan.digest()),
            "approved": str(approved) if approved else None,
            "tasks": [{"id": task.id, "title": task.title} for task in plan.tasks],
            "open_questions": list(plan.open_questions),
            "tool_calls": result.tool_calls,
            "tokens": result.tokens,
        },
        json_mode=json_mode,
        human="\n".join(human),
    )


@app.command("approve")
def approve_plan(
    plan_file: Annotated[
        Path | None,
        typer.Argument(metavar="PLAN.md", help="The plan to approve; a digest also works."),
    ] = None,
    root: ProjectRoot = Path(),
    digest: Annotated[
        str | None, typer.Option("--digest", help="Approve a stored plan by digest.")
    ] = None,
    json_mode: JsonOption = False,
) -> None:
    """Approve a plan, which is what `nih implement` then runs.

    Approval is a human action and stays one: this command only records a
    decision somebody made, and it records it by digest, so what gets built is
    what was read.
    """
    config = HarnessConfig.load(root.resolve(), profile=None)
    store = PlanStore(config)
    if digest is not None:
        try:
            plan, _ = store.load(Digest(digest))
        except NoSuchPlanError as error:
            fail(f"no stored plan with digest {digest}", json_mode=json_mode)
            raise typer.Exit(1) from error
    else:
        source = plan_file or Path("PLAN.md")
        if not source.is_file():
            fail(f"no plan at {source}", json_mode=json_mode)
            raise typer.Exit(1)
        try:
            plan, _ = store.submit(source.read_text(encoding="utf-8"))
        except (ArtifactNotFoundError, ValidationError) as error:
            fail(f"{source}: invalid plan", {"detail": str(error)}, json_mode=json_mode)
            raise typer.Exit(1) from error
    target = store.approve(plan.digest())
    emit(
        {
            "ok": True,
            "approved": str(target),
            "plan_digest": str(plan.digest()),
            "tasks": len(plan.tasks),
        },
        json_mode=json_mode,
        human=(
            f"approved {plan.digest()} ({len(plan.tasks)} task(s)) -> {target}\n"
            "  next: nih implement"
        ),
    )


@app.command("check")
def check(
    plan_file: Annotated[Path, typer.Argument(help="Path to PLAN.md")],
    json_mode: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Validate a plan against the Plan schema. This is the plan gate, offline."""
    try:
        text = plan_file.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"cannot read {plan_file}: {error}", json_mode=json_mode)
        raise typer.Exit(1) from error
    try:
        parsed = parse_plan(text)
    except (ArtifactNotFoundError, ValidationError) as error:
        fail(f"{plan_file}: invalid plan", {"detail": str(error)}, json_mode=json_mode)
        raise typer.Exit(1) from error
    human = [
        f"{plan_file}: valid",
        f"  tasks: {len(parsed.tasks)}",
        f"  spec digest: {parsed.spec_digest}",
    ]
    human += [
        f"  {index}. {task.id}: {task.title}"
        for index, task in enumerate(parsed.tasks, start=1)
    ]
    emit(
        {
            "ok": True,
            "valid": True,
            "plan_digest": str(parsed.digest()),
            "spec_digest": str(parsed.spec_digest),
            "tasks": [{"id": task.id, "title": task.title} for task in parsed.tasks],
        },
        json_mode=json_mode,
        human="\n".join(human),
    )


@app.command("import")
def import_plan(
    source: Annotated[Path, typer.Argument(help="A plan written outside the harness")],
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write PLAN.md")
    ] = Path(
        "PLAN.md",
    ),
) -> None:
    """Convert an external plan into the Plan schema.

    A document that already conforms is accepted and normalised offline. One
    that does not needs the planner role, and therefore a model.
    """
    text = source.read_text(encoding="utf-8")
    try:
        parsed = parse_plan(text)
    except (ArtifactNotFoundError, ValidationError):
        unavailable(
            "plan import",
            "the planner role, to convert a plan that does not already conform;\n"
            "a plan that does conform is imported by this command today",
            "docs/roadmap.md, stage 'plan'",
        )
        return
    output.write_text(render_plan(parsed), encoding="utf-8")
    typer.secho(f"{output}: written from {source}", fg=typer.colors.GREEN)
    typer.echo(f"  tasks: {len(parsed.tasks)}")

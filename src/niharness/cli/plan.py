# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih plan`: the planning stage and its two offline helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from typer.core import TyperGroup

from niharness.cli.output import emit, fail
from niharness.cli.unavailable import unavailable
from niharness.domain.markdown import ArtifactNotFoundError, parse_plan, render_plan

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


@app.command(DEFAULT_COMMAND)
def plan_spec(
    spec: Annotated[Path, typer.Argument(help="Path to SPEC.md")],
) -> None:
    """Run the planning stage on a spec, producing PLAN.md for approval."""
    del spec
    unavailable(
        "plan",
        "the inference adapter and the MCP tool provider (read-only tools for the planner)",
        "docs/roadmap.md, stage 'plan'",
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
            "the inference adapter (the planner role converts free-form plans)",
            "docs/roadmap.md, stage 'plan'",
        )
        return
    output.write_text(render_plan(parsed), encoding="utf-8")
    typer.secho(f"{output}: written from {source}", fg=typer.colors.GREEN)
    typer.echo(f"  tasks: {len(parsed.tasks)}")

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih run`: the whole pipeline, without the approval stop.

Both halves exist on their own - `nih plan` and `nih implement` - and each is
a fresh run seeded only by its artifact. This command chains them, which means
it does exactly two things the two commands do not: it approves the plan on
your behalf, and it decides what to do when the planner says it had to guess.

The second one is the reason this file has an opinion. The only stop `nih run`
removes is the human reading of the plan, so a plan whose `open_questions` are
not empty is precisely the plan that stop was for. It stops anyway, prints the
questions, and leaves the plan approved and ready for `nih implement`; the
`--ignore-open-questions` flag is there for the case where you have read them
and decided they do not matter.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import typer

from niharness.adapters.local_http import AirGapViolationError, explain
from niharness.adapters.mcp import ExternalToolsError
from niharness.application import RunResult
from niharness.cli import implement as implement_module
from niharness.cli import plan as plan_module
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.config import HarnessConfig
from niharness.domain.artifacts import Plan, Spec
from niharness.domain.markdown import render_plan
from niharness.management.plans import PlanStore

EX_UNAVAILABLE = 69
"""`sysexits.h`: a service the command needs is not there."""

TIMEOUT_HINT = "On a slow host raise [models.main] request_timeout_seconds."
"""What to suggest when the model stops answering mid-generation."""


def run(
    config: HarnessConfig,
    spec_file: Path,
    *,
    local_tools: bool,
    ignore_open_questions: bool,
    json_mode: bool,
) -> None:
    """Plan `spec_file`, approve the plan, and implement it.

    Raises:
        typer.Exit: when either half fails, or when the plan has open
            questions and nobody has said to ignore them.
    """
    spec = plan_module.load_spec(spec_file, json_mode=json_mode)
    if not json_mode:
        typer.secho(f"planning {spec_file}", bold=True)
    planned = _planned(config, spec, local_tools=local_tools, json_mode=json_mode)

    store = PlanStore(config)
    store.submit(render_plan(planned))
    approved = store.approve(planned.digest())

    if planned.open_questions and not ignore_open_questions:
        fail(
            "the planner had to assume past "
            f"{len(planned.open_questions)} open question(s), and `nih run` is the "
            "one command with no stop for a human to catch that. The plan is "
            f"approved at {approved}: read those questions, then `nih implement`, "
            "or re-run with --ignore-open-questions.",
            {
                "plan_digest": str(planned.digest()),
                "approved": str(approved),
                "open_questions": list(planned.open_questions),
            },
            json_mode=json_mode,
        )
        raise typer.Exit(1)

    if not json_mode:
        typer.secho(f"\nimplementing {len(planned.tasks)} task(s)", bold=True)
    result = _implemented(config, planned, local_tools=local_tools, json_mode=json_mode)

    if json_mode:
        emit(
            {
                "ok": result.completed,
                "plan_digest": str(planned.digest()),
                "approved": str(approved),
                "open_questions": list(planned.open_questions),
                "run": result.model_dump(mode="json"),
            },
            json_mode=True,
        )
    else:
        implement_module.report(result, config.runs_dir)
    if not result.completed:
        raise typer.Exit(1)


def _planned(
    config: HarnessConfig,
    spec: Spec,
    *,
    local_tools: bool,
    json_mode: bool,
) -> Plan:
    """Run the planning half and return its plan.

    Raises:
        typer.Exit: when planning fails or produces nothing to implement.
    """
    result = _guarded(
        lambda: blocking(
            plan_module.execute(config, spec, local_tools=local_tools, quiet=json_mode),
        ),
        json_mode=json_mode,
    )
    if result.plan is None:
        fail(
            f"no plan, so nothing to implement: {result.reason}",
            {"run_id": str(result.run_id), "outcome": result.outcome.value},
            json_mode=json_mode,
        )
        raise typer.Exit(1)
    return result.plan


def _implemented(
    config: HarnessConfig,
    plan: Plan,
    *,
    local_tools: bool,
    json_mode: bool,
) -> RunResult:
    """Run the implementing half."""
    return _guarded(
        lambda: blocking(
            implement_module.execute(config, plan, local_tools=local_tools, quiet=json_mode),
        ),
        json_mode=json_mode,
    )


def _guarded[T](work: Callable[[], T], *, json_mode: bool) -> T:
    """Run one half of the pipeline, reporting the three ways a service fails.

    Both halves reach the same model, the same declared servers and the same
    sandbox, so they fail the same way; saying so once keeps the two stages'
    error messages from drifting apart.

    Raises:
        typer.Exit: when a service the command needs is not there.
    """
    try:
        return work()
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

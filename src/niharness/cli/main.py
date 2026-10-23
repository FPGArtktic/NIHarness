# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The `nih` command line.

Commands that only need pure logic or the filesystem work today. Commands that
need a model, a container or a vector store report what is missing and exit
non-zero; see `docs/roadmap.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from niharness import __version__
from niharness.cli import docs as docs_module
from niharness.cli import gate as gate_module
from niharness.cli import implement as implement_module
from niharness.cli import ingest as ingest_module
from niharness.cli import init as init_module
from niharness.cli import logging as logging_module
from niharness.cli import plan as plan_module
from niharness.cli import runs as runs_module
from niharness.cli import status as status_module
from niharness.cli import tools as tools_module
from niharness.cli.unavailable import unavailable
from niharness.config import HarnessConfig
from niharness.domain.gate import GateTier
from niharness.management import server as management_server

app = typer.Typer(
    name="nih",
    help="NIHarness: an air-gapped, gated, event-sourced multi-agent coding harness.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(plan_module.app, name="plan")

docs_app = typer.Typer(help="Documentation and generated reference material.")
app.add_typer(docs_app, name="docs")

ProjectRoot = Annotated[
    Path,
    typer.Option("--project-root", "-C", help="The project to operate on.", show_default="."),
]

JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print one JSON object instead of the human form."),
]


def _config(root: Path, profile: str | None) -> HarnessConfig:
    """Load configuration for `root`."""
    return HarnessConfig.load(root, profile=profile)


@app.callback()
def main_callback(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Configure logging for every command."""
    logging_module.configure(verbose=verbose)


@app.command()
def version() -> None:
    """Print the harness version."""
    typer.echo(__version__)


@app.command()
def init(
    root: ProjectRoot = Path(),
    force: Annotated[bool, typer.Option("--force", help="Overwrite harness.toml.")] = False,
) -> None:
    """Probe the hardware, pick a profile and write harness.toml."""
    init_module.init(root.resolve(), force=force)


@app.command()
def config(
    root: ProjectRoot = Path(),
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
) -> None:
    """Print the merged configuration, as the harness sees it."""
    typer.echo(_config(root.resolve(), profile).model_dump_json(indent=2))


@app.command()
def gate(
    root: ProjectRoot = Path(),
    tier: Annotated[
        GateTier, typer.Option("--tier", help="Which tier to plan.")
    ] = GateTier.QUICK,
    changed: Annotated[
        list[str] | None,
        typer.Option("--changed", help="Paths changed since the last green commit."),
    ] = None,
    json_mode: JsonOption = False,
) -> None:
    """Show the commands the gate would run in this repository."""
    gate_module.gate(root.resolve(), tier, tuple(changed or ()), json_mode=json_mode)


@app.command()
def status(
    run_id: Annotated[str | None, typer.Argument(help="Run id; omit to list runs.")] = None,
    root: ProjectRoot = Path(),
    json_mode: JsonOption = False,
) -> None:
    """Fold a run's event log into a snapshot."""
    status_module.status(_config(root.resolve(), None), run_id, json_mode=json_mode)


@app.command()
def runs(root: ProjectRoot = Path(), json_mode: JsonOption = False) -> None:
    """List this project's runs, with what each one is doing."""
    runs_module.runs(_config(root.resolve(), None), json_mode=json_mode)


@app.command()
def ingest(
    documents: Annotated[list[Path], typer.Argument(help="Documents to read.")],
    root: ProjectRoot = Path(),
    languages: Annotated[
        str,
        typer.Option("--ocr-languages", help="OCR languages, the engine's way: pol+eng."),
    ] = "eng",
    dpi: Annotated[int, typer.Option("--dpi", help="Rendering resolution for OCR.")] = 300,
    index: Annotated[
        bool,
        typer.Option("--index/--no-index", help="Index the chunks into the vector store."),
    ] = True,
    json_mode: JsonOption = False,
) -> None:
    """Read documents into citable chunks, with OCR where there is no text layer."""
    ingest_module.ingest(
        _config(root.resolve(), None),
        documents,
        languages=languages,
        dpi=dpi,
        index=index,
        json_mode=json_mode,
    )


@app.command()
def tools(
    root: ProjectRoot = Path(),
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
    json_mode: JsonOption = False,
) -> None:
    """Show the tools a run would offer, per role, including declared MCP servers.

    Declared servers are started to ask them what they offer, so a server that
    would fail during a run fails here instead.
    """
    tools_module.tools(_config(root.resolve(), profile), json_mode=json_mode)


@app.command()
def cancel(
    run_id: Annotated[str, typer.Argument(help="Run id to stop")],
    root: ProjectRoot = Path(),
    reason: Annotated[str, typer.Option("--reason", help="Recorded in the event log.")] = "",
    json_mode: JsonOption = False,
) -> None:
    """Ask a run to stop at its next step boundary."""
    runs_module.cancel(_config(root.resolve(), None), run_id, reason, json_mode=json_mode)


@docs_app.command("generate")
def docs_generate(root: ProjectRoot = Path()) -> None:
    """Regenerate JSON Schemas and the generated reference pages."""
    docs_module.generate(root.resolve(), app)


@app.command()
def mcp(
    root: ProjectRoot = Path(),
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
) -> None:
    """Serve the management surface over stdio, for an agent to drive this project.

    It can start, stop and inspect runs and approve plans. It cannot read
    project files, patch, commit, or run the gate: the work belongs to the
    models in the sandbox, and the gate is the only way in.
    """
    management_server.serve(_config(root.resolve(), profile))


@app.command()
def brainstorm(root: ProjectRoot = Path()) -> None:
    """Entry A: an interactive session that writes SPEC.md."""
    del root
    unavailable(
        "brainstorm",
        "an interactive session loop and a TUI pane for the running draft;\n"
        "the model, the read-only tools and the prompt layer are built",
        "docs/roadmap.md, stage 'brainstorm'",
    )


@app.command()
def implement(
    root: ProjectRoot = Path(),
    plan_file: Annotated[
        Path | None,
        typer.Option("--plan", help="Execute this plan instead of the approved one."),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
    local_tools: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-tools",
            help="Run the tool server on this host instead of in the sandbox.",
        ),
    ] = False,
    detached: Annotated[
        bool,
        typer.Option("--detach", help="Start the run in the background and print its id."),
    ] = False,
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Use this run id.", hidden=True),
    ] = None,
    json_mode: JsonOption = False,
) -> None:
    """Execute an approved plan, task by task, through the gate."""
    implement_module.implement(
        _config(root.resolve(), profile),
        plan_file,
        local_tools=local_tools,
        detached=detached,
        run_id=run_id,
        json_mode=json_mode,
    )


@app.command()
def run(spec: Annotated[Path, typer.Argument(help="Path to SPEC.md")]) -> None:
    """Plan and implement without stopping for approval."""
    del spec
    unavailable(
        "run",
        "the planning stage, which this chains to implement without an\n"
        "approval stop; `nih implement --plan` runs the second half today",
        "docs/roadmap.md, stage 'implement'",
    )


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="Run id to continue")],
    root: ProjectRoot = Path(),
    profile: Annotated[
        str | None, typer.Option("--profile", help="Override the profile.")
    ] = None,
    local_tools: Annotated[
        bool,
        typer.Option(
            "--unsafe-local-tools",
            help="Run the tool server on this host instead of in the sandbox.",
        ),
    ] = False,
    json_mode: JsonOption = False,
) -> None:
    """Continue a run that was cancelled or stopped short of its plan."""
    implement_module.resume(
        _config(root.resolve(), profile),
        run_id,
        local_tools=local_tools,
        json_mode=json_mode,
    )


@app.command()
def compact(run_id: Annotated[str, typer.Argument(help="Run id to compact")]) -> None:
    """Compact a run's window now, instead of waiting for the threshold."""
    del run_id
    unavailable(
        "compact",
        "compaction of a stopped run on demand; a live run already compacts\n"
        "its own window with the tokenizer and the utility model",
        "docs/roadmap.md, 'context management'",
    )


@app.command()
def research(topic: Annotated[str, typer.Argument(help="What to research")]) -> None:
    """Run a read-only researcher sub-run over the index."""
    del topic
    unavailable(
        "research",
        "the researcher sub-run and its scratch worktree; the store it would\n"
        "cite is built, and `rag_query` already answers from it",
        "docs/roadmap.md, 'retrieval'",
    )


@app.command()
def bench(
    layer: Annotated[str, typer.Option("--layer", help="L0, L1, L2 or L3.")] = "L0",
) -> None:
    """Run a benchmark layer and append JSONL records."""
    del layer
    unavailable(
        "bench",
        "the runner; the suites carry a task format and a seed set, and the\n"
        "L0 numbers in benchmarks/results/ were taken by hand",
        "docs/benchmarks/methodology.md",
    )


def main() -> None:
    """Entry point for the `nih` script."""
    app()

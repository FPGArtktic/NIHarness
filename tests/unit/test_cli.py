# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The commands that work without a model, a container or a vector store."""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from niharness import __version__
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.cli import docs as docs_module
from niharness.cli.main import app
from niharness.cli.unavailable import EX_UNAVAILABLE
from niharness.domain import events as ev
from niharness.domain.artifacts import Plan, Task
from niharness.domain.identity import Digest, RunId
from niharness.domain.markdown import render_plan

runner = CliRunner()


def plan() -> Plan:
    return Plan(
        spec_digest=Digest("0123456789abcdef"),
        tasks=(Task(id="t1", title="Add the parser", intent="parse the artifact block"),),
    )


def test_version_is_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_config_prints_the_merged_configuration(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "-C", str(tmp_path)])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["sandbox"]["network"] == "none"
    assert parsed["compact"]["threshold"] == 0.75


def test_gate_plans_the_quick_tier_for_this_repository() -> None:
    result = runner.invoke(app, ["gate", "--tier", "quick"])
    assert result.exit_code == 0
    assert "python" in result.output
    assert "ruff format --check ." in result.output
    assert "pytest" in result.output


def test_gate_refuses_a_directory_that_is_not_a_repository(tmp_path: Path) -> None:
    result = runner.invoke(app, ["gate", "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "not a git repository" in result.output


def test_plan_check_accepts_a_conforming_plan(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text(render_plan(plan()), encoding="utf-8")
    result = runner.invoke(app, ["plan", "check", str(target)])
    assert result.exit_code == 0
    assert "valid" in result.output
    assert "t1: Add the parser" in result.output


def test_plan_check_rejects_prose(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text("# Plan\n\nFirst do the thing.\n", encoding="utf-8")
    result = runner.invoke(app, ["plan", "check", str(target)])
    assert result.exit_code == 1
    assert "invalid plan" in result.output


def test_plan_check_reports_a_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["plan", "check", str(tmp_path / "absent.md")])
    assert result.exit_code == 1
    assert "cannot read" in result.output


def test_plan_import_normalises_a_conforming_plan(tmp_path: Path) -> None:
    source = tmp_path / "claude-plan.md"
    source.write_text(plan().model_dump_json(), encoding="utf-8")
    target = tmp_path / "PLAN.md"
    result = runner.invoke(app, ["plan", "import", str(source), "-o", str(target)])
    assert result.exit_code == 0
    assert "## 1. Add the parser" in target.read_text(encoding="utf-8")


def test_plan_import_needs_a_model_for_free_form_prose(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("We should refactor the gate, then add tests.\n", encoding="utf-8")
    result = runner.invoke(app, ["plan", "import", str(source)])
    assert result.exit_code == EX_UNAVAILABLE
    assert "planner" in result.output


@pytest.mark.parametrize(
    ("command", "needs"),
    [
        (["brainstorm"], "inference"),
        (["implement"], "sandbox"),
        (["run", "SPEC.md"], "pipeline"),
        (["resume", "abc"], "run loop"),
        (["compact", "abc"], "tokenizer"),
        (["research", "topic"], "vector store"),
        (["bench"], "benchmark"),
    ],
)
def test_unimplemented_commands_say_what_they_need(command: list[str], needs: str) -> None:
    result = runner.invoke(app, command)
    assert result.exit_code == EX_UNAVAILABLE
    assert "not available yet" in result.output
    assert needs in result.output


async def seed_run(runs_dir: Path, run_id: RunId) -> None:
    sink = JsonlEventSink(runs_dir, run_id)
    await sink.append(
        ev.RunStarted(branch=f"harness/run-{run_id}", profile="cpu-only", model="m")
    )
    await sink.append(
        ev.VcsCommitted(
            sha="cafe", subject="feat: x", files_changed=1, insertions=1, deletions=0
        ),
    )


def test_status_folds_a_run(tmp_path: Path) -> None:
    """The command is synchronous, so the fixture is seeded in its own loop."""
    run_id = RunId("8f21c0")
    asyncio.run(seed_run(tmp_path / ".nih" / "runs", run_id))

    listed = runner.invoke(app, ["status", "-C", str(tmp_path)])
    assert listed.exit_code == 0
    assert "8f21c0" in listed.output

    shown = runner.invoke(app, ["status", "8f21c0", "-C", str(tmp_path)])
    assert shown.exit_code == 0
    assert "commits" in shown.output
    assert "events" in shown.output


def test_status_reports_an_unknown_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", "nope", "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "no event log" in result.output


def test_the_generated_files_in_the_tree_are_up_to_date(tmp_path: Path) -> None:
    """Generated pages and schemas are committed; the gate fails when they are stale.

    The comparison runs against a scratch directory, so the test observes the
    tree without changing it - a test that regenerated the files would pass by
    repairing what it was supposed to catch.
    """
    docs_module.generate(tmp_path, app)
    repo = Path(__file__).resolve().parents[2]
    generated = [
        path.relative_to(tmp_path) for path in sorted(tmp_path.rglob("*")) if path.is_file()
    ]
    assert generated, "nothing was generated"
    stale = [
        str(relative)
        for relative in generated
        if (repo / relative).read_bytes() != (tmp_path / relative).read_bytes()
    ]
    assert not stale, f"run `nih docs generate`: {', '.join(stale)}"

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
    parsed = json.loads(result.stdout)
    assert parsed["sandbox"]["network"] == "none"
    assert parsed["compact"]["threshold"] == 0.75


def test_tools_lists_what_each_role_may_call(tmp_path: Path) -> None:
    result = runner.invoke(app, ["tools", "-C", str(tmp_path), "--json"])
    assert result.exit_code == 0
    parsed = json.loads(result.stdout)
    assert "git_commit" in parsed["builtin"]["implementer"]
    assert "git_commit" not in parsed["builtin"]["reviewer"], "the reviewer does not commit"
    assert parsed["servers"] == [], "no project declares one by default"


def test_tools_reports_a_declared_server_it_cannot_start(tmp_path: Path) -> None:
    (tmp_path / "harness.toml").write_text(
        "[[mcp.servers]]\n"
        'name = "fpga"\n'
        'command = "nih-no-such-mcp-server"\n'
        'tools = ["read_report"]\n'
        'roles = ["implementer"]\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["tools", "-C", str(tmp_path), "--json"])
    assert result.exit_code == EX_UNAVAILABLE
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is False
    assert "nih-no-such-mcp-server" in parsed["error"]


def test_a_declared_server_must_say_which_roles_get_it(tmp_path: Path) -> None:
    (tmp_path / "harness.toml").write_text(
        "[[mcp.servers]]\n"
        'name = "fpga"\n'
        'command = "/bin/true"\n'
        'tools = ["read_report"]\n'
        "roles = []\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "-C", str(tmp_path)])
    assert result.exit_code != 0


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


STUBS = [
    (["brainstorm"], "session loop"),
    (["run", "SPEC.md"], "planning stage"),
    (["compact", "abc"], "on demand"),
    (["research", "topic"], "sub-run"),
    (["plan", "spec", "SPEC.md"], "runner"),
]
"""Each stub and a phrase from the thing that is actually missing."""

PRESENT = ("built", "today", "already")
"""A stub has to say what *is* there, which is the part that goes stale."""


@pytest.mark.parametrize(("command", "needs"), STUBS)
def test_unimplemented_commands_say_what_they_need(command: list[str], needs: str) -> None:
    result = runner.invoke(app, command)
    assert result.exit_code == EX_UNAVAILABLE
    assert "not available yet" in result.output
    assert needs in result.output


@pytest.mark.parametrize(("command", "needs"), STUBS)
def test_a_stub_distinguishes_what_is_missing_from_what_is_built(
    command: list[str],
    needs: str,
) -> None:
    """The previous version of this test pinned a noun, and the noun went stale.

    Five of six stubs ended up naming a prerequisite that had since shipped -
    `nih research` asked for the vector store adapter while `rag_query` was
    answering from it - because nothing made the message account for the
    present. Requiring each to say what is already there is what stops that:
    a message cannot claim a dependency is missing and also describe it as
    built without somebody noticing.
    """
    del needs
    result = runner.invoke(app, command)
    assert any(word in result.output for word in PRESENT), (
        f"{command[0]} says what it needs but not what already works: {result.output}"
    )


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


def test_runs_lists_nothing_when_there_are_no_runs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["runs", "-C", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"ok": True, "runs": []}


def test_runs_reports_what_a_run_is_doing(tmp_path: Path) -> None:
    run_id = RunId("20261022-090000-aaaaaa")
    asyncio.run(seed_run(tmp_path / ".nih" / "runs", run_id))
    parsed = json.loads(runner.invoke(app, ["runs", "-C", str(tmp_path), "--json"]).stdout)
    entry = parsed["runs"][0]
    assert entry["run_id"] == run_id
    assert entry["outcome"] == "running", "no terminal event was appended"
    assert entry["live"] is False, "no process owns it"
    assert entry["commits"] == 1


def test_cancel_writes_to_the_inbox(tmp_path: Path) -> None:
    run_id = RunId("20261022-090000-bbbbbb")
    runs_dir = tmp_path / ".nih" / "runs"
    asyncio.run(seed_run(runs_dir, run_id))
    result = runner.invoke(
        app,
        ["cancel", run_id, "-C", str(tmp_path), "--reason", "enough", "--json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"] is True
    inbox = (runs_dir / run_id / "control.jsonl").read_text(encoding="utf-8")
    assert '"cancel_run"' in inbox
    assert "enough" in inbox


def test_cancelling_an_unknown_run_fails_in_the_requested_shape(tmp_path: Path) -> None:
    result = runner.invoke(app, ["cancel", "nope", "-C", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"ok": False, "error": "no run nope", "run_id": "nope"}


def test_status_json_carries_the_folded_snapshot(tmp_path: Path) -> None:
    run_id = RunId("20261022-090000-cccccc")
    asyncio.run(seed_run(tmp_path / ".nih" / "runs", run_id))
    parsed = json.loads(
        runner.invoke(app, ["status", run_id, "-C", str(tmp_path), "--json"]).stdout,
    )
    assert parsed["ok"] is True
    assert parsed["live"] is False
    assert parsed["snapshot"]["run_id"] == run_id
    assert parsed["snapshot"]["commits"] == ["cafe"]


def test_gate_json_lists_the_commands() -> None:
    parsed = json.loads(runner.invoke(app, ["gate", "--json"]).stdout)
    assert parsed["ok"] is True
    assert "python" in parsed["languages"]
    names = [command["name"] for command in parsed["commands"]]
    assert "ruff-format" in names
    assert parsed["commands"][0]["argv"][0] == "ruff"


def test_plan_check_json_names_the_tasks(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text(render_plan(plan()), encoding="utf-8")
    parsed = json.loads(runner.invoke(app, ["plan", "check", str(target), "--json"]).stdout)
    assert parsed["valid"] is True
    assert parsed["tasks"] == [{"id": "t1", "title": "Add the parser"}]
    assert len(parsed["plan_digest"]) == 16


def test_plan_check_json_reports_an_invalid_plan(tmp_path: Path) -> None:
    target = tmp_path / "PLAN.md"
    target.write_text("# Plan\n\nprose only\n", encoding="utf-8")
    result = runner.invoke(app, ["plan", "check", str(target), "--json"])
    assert result.exit_code == 1
    parsed = json.loads(result.stdout)
    assert parsed["ok"] is False
    assert "invalid plan" in parsed["error"]


def test_resuming_an_unknown_run_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(app, ["resume", "nope", "-C", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "nope"


def test_resuming_a_finished_run_says_there_is_nothing_to_do(tmp_path: Path) -> None:
    run_id = RunId("20261022-090000-dddddd")
    runs_dir = tmp_path / ".nih" / "runs"
    asyncio.run(seed_run(runs_dir, run_id))
    asyncio.run(
        JsonlEventSink(runs_dir, run_id).append(
            ev.RunFinished(outcome=ev.RunOutcome.COMPLETED, reason="done"),
        ),
    )
    result = runner.invoke(app, ["resume", run_id, "-C", str(tmp_path), "--json"])
    assert result.exit_code == 1
    assert "nothing to continue" in json.loads(result.stdout)["error"]


def test_ingest_refuses_a_document_that_is_not_there(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", str(tmp_path / "absent.pdf"), "-C", str(tmp_path)])
    assert result.exit_code == 1
    assert "no such document" in result.output


def test_ingest_writes_chunks_a_retriever_can_cite(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("ps2pdf") is None or shutil.which("pdftotext") is None:
        pytest.skip("ghostscript or poppler is not installed")
    source = tmp_path / "doc.ps"
    source.write_text(
        "%!PS\n/Helvetica findfont 14 scalefont setfont\n"
        "72 700 moveto (The loop bandwidth is programmable between 1 and 16 kHz.) show\n"
        "72 680 moveto (The default is four kilohertz, which suits most receivers.) show\n"
        "showpage\n",
        encoding="utf-8",
    )
    document = tmp_path / "manual.pdf"
    subprocess.run(["ps2pdf", str(source), str(document)], check=True, capture_output=True)

    result = runner.invoke(
        app,
        ["ingest", str(document), "-C", str(tmp_path), "--no-index", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["indexed"] is False, "--no-index leaves the chunks on disk only"
    entry = payload["documents"][0]
    assert entry["chunks"] >= 1
    assert "p. 1" in entry["citations"][0], "a chunk cites the page it came from"
    corpus = Path(entry["corpus"])
    assert corpus.is_file()
    first = json.loads(corpus.read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "doc"
    assert first["first_page"] == 1
    assert first["start_line"] is None, "a page is not a line"
    assert "loop bandwidth" in first["text"]
    assert first["path"] == "manual.pdf", (
        "a citation names the document the way a reader can follow it, "
        "not by somebody's absolute path"
    )


def test_a_document_outside_the_project_keeps_its_path(tmp_path: Path) -> None:
    """It has no project-relative name, and inventing one points at nothing."""
    from niharness.cli.ingest import cited_as

    project = tmp_path / "project"
    project.mkdir()
    assert cited_as(project / "docs" / "manual.pdf", project) == "docs/manual.pdf"
    assert cited_as(tmp_path / "elsewhere.pdf", project) == str(tmp_path / "elsewhere.pdf")


def test_ingest_says_so_when_it_cannot_index_what_it_read(tmp_path: Path) -> None:
    """A document read but not indexed is a failure, not a success with a caveat.

    The chunks are on disk either way, so nothing is lost - but a command that
    reported success here would leave an operator believing `rag_query` can
    find a document that nothing indexed.
    """
    import shutil
    import subprocess

    if shutil.which("ps2pdf") is None or shutil.which("pdftotext") is None:
        pytest.skip("ghostscript or poppler is not installed")
    source = tmp_path / "doc.ps"
    source.write_text(
        "%!PS\n/Helvetica findfont 14 scalefont setfont\n"
        "72 700 moveto (One page is enough for this.) show\nshowpage\n",
        encoding="utf-8",
    )
    document = tmp_path / "manual.pdf"
    subprocess.run(["ps2pdf", str(source), str(document)], check=True, capture_output=True)

    result = runner.invoke(app, ["ingest", str(document), "-C", str(tmp_path), "--json"])
    assert result.exit_code == EX_UNAVAILABLE
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "the chunks are written" in payload["error"]
    assert "models.embeddings" in payload["error"]
    corpus = next((tmp_path / ".nih" / "corpus").glob("*.jsonl"))
    assert corpus.read_text(encoding="utf-8").strip(), "written before indexing was attempted"


def test_a_record_calls_the_tree_dirty_for_the_code_not_for_its_own_output(
    tmp_path: Path,
) -> None:
    """Writing a record makes the tree dirty, which is not what the flag means.

    Asked per pass, every pass after the first reported `true` - the flag
    answering a question about its own output rather than about the code. It
    is measured once now, with the results directory excluded.
    """
    import subprocess

    from niharness.cli.bench import _dirty

    repo = tmp_path / "repo"
    (repo / "benchmarks" / "results").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    for argv in (
        ("git", "init", "-q", "."),
        ("git", "config", "user.email", "t@example.com"),
        ("git", "config", "user.name", "T"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "init"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    assert _dirty(repo) is False, "a clean tree is clean"

    (repo / "benchmarks" / "results" / "l1.jsonl").write_text("{}\n", encoding="utf-8")
    assert _dirty(repo) is False, "its own record does not make the code uncommitted"

    (repo / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _dirty(repo) is True, "a change to the code does"


def test_the_generated_cli_page_does_not_depend_on_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page pins its width, whatever the environment thinks it is.

    Typer formats help through rich and takes the width from its own
    `rich_utils.MAX_WIDTH`; `COLUMNS` is never consulted. The first attempt at
    fixing this set `COLUMNS` and was a no-op that passed locally because the
    default already happened to be 80 - while a CI runner, where rich picks a
    much wider default of its own, produced 188 columns and a page that could
    never match the committed one.

    So the hostile value is set where it is actually read.
    """
    from typer import rich_utils

    from niharness.cli.docs import HELP_WIDTH, render_cli_page
    from niharness.cli.main import app as real_app

    rendered = []
    for width in (40, 250):
        monkeypatch.setattr(rich_utils, "MAX_WIDTH", width)
        monkeypatch.setenv("COLUMNS", str(width))
        rendered.append(render_cli_page(real_app))
    assert rendered[0] == rendered[1], "the page must not move with the terminal"
    assert max(len(line) for line in rendered[0].splitlines()) <= HELP_WIDTH


def test_the_width_is_put_back_after_generating() -> None:
    """A user's own `--help` must not inherit the page's width."""
    from typer import rich_utils

    from niharness.cli.docs import render_cli_page
    from niharness.cli.main import app as real_app

    before = rich_utils.MAX_WIDTH
    render_cli_page(real_app)
    assert before == rich_utils.MAX_WIDTH

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The L3 suite's invariants, and the pieces that stage and grade a task."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from niharness.cli.bench import SuiteError, hidden_verdict, load_l3, plan_of, stage_hidden

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "benchmarks" / "suites" / "l3"


def run_in(work: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *argv],
        cwd=work,
        check=False,
        capture_output=True,
        text=True,
    )


def test_the_suite_declares_tasks_with_hidden_tests_for_each() -> None:
    tasks, metrics = load_l3(ROOT)
    assert tasks, "the suite declares tasks"
    assert "pass_at_1_ratio" in metrics, "the layer's own measure is recorded"
    for task in tasks:
        assert task.acceptance, f"{task.id} says what done means"
        staged = sorted((SUITE / "hidden" / task.id).glob("test_*.py"))
        assert staged, f"{task.id} has tests it is graded by"


def test_a_task_becomes_a_one_task_plan_bound_to_a_digest_of_itself() -> None:
    """A plan's spec digest ties it to a spec, and a benchmark task has none."""
    tasks, _ = load_l3(ROOT)
    plan = plan_of(tasks[0])
    assert len(plan.tasks) == 1
    assert plan.tasks[0].acceptance == tasks[0].acceptance, "the brief reaches the model"
    assert plan_of(tasks[0]).spec_digest == plan.spec_digest, "the same task, the same digest"
    assert plan_of(tasks[1]).spec_digest != plan.spec_digest


def test_a_missing_suite_is_named_rather_than_crashed_through(tmp_path: Path) -> None:
    with pytest.raises(SuiteError, match="no L3 suite"):
        load_l3(tmp_path)


def test_the_fixture_is_green_and_the_hidden_tests_are_not(tmp_path: Path) -> None:
    """The invariant the whole layer rests on, in two halves.

    The fixture must be green, or a task starts from a red tree and the gate
    stops work that was never wrong. And every hidden test must **fail** on
    that untouched fixture: a test that already passes grades nothing, and a
    suite of them would report `pass_at_1_ratio` 1.0 for a model that did
    nothing at all.
    """
    tasks, _ = load_l3(ROOT)
    baseline = tmp_path / "_baseline"
    shutil.copytree(SUITE / "fixture", baseline)
    for argv in (("ruff", "format", "--check", "."), ("ruff", "check", "."), ("pytest", "-q")):
        done = run_in(baseline, *argv)
        assert done.returncode == 0, f"the fixture must be green: {argv}\n{done.stdout[-600:]}"

    already_passing: list[str] = []
    for task in tasks:
        work = tmp_path / task.id
        shutil.copytree(SUITE / "fixture", work)
        staged = stage_hidden(ROOT, task, work)
        assert staged > 0, f"{task.id}: nothing was staged"
        passed, tail = hidden_verdict(work)
        if passed:
            already_passing.append(f"{task.id}: {tail}")
    assert not already_passing, (
        "a hidden test that passes before the work is done grades nothing:\n"
        + "\n".join(already_passing)
    )


def test_the_hidden_tests_land_in_the_tree_and_the_grade_reads_them(tmp_path: Path) -> None:
    """`stage_hidden` puts them where the fixture's own pytest will collect them."""
    tasks, _ = load_l3(ROOT)
    work = tmp_path / "repo"
    shutil.copytree(SUITE / "fixture", work)
    stage_hidden(ROOT, tasks[0], work)
    assert (work / "tests" / "test_hidden.py").is_file()
    passed, tail = hidden_verdict(work)
    assert passed is False
    assert tail, "the grade says what it saw, for the record's notes"

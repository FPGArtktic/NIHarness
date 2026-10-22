# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The gate decides what runs, in what order, and what a failure is called."""

from niharness.domain.gate import (
    GATE_MATRIX,
    KIND_ORDER,
    GateKind,
    GateResult,
    GateStepResult,
    GateTier,
    Language,
    detect_languages,
    format_commands,
    gate_listing,
    normalize_output,
    plan_gate,
)


def test_languages_are_detected_by_marker_file() -> None:
    detected = detect_languages(["pyproject.toml", "src/a.py", "tui/Cargo.toml", "mkdocs.yml"])
    assert detected == frozenset({Language.PYTHON, Language.RUST, Language.DOCS})


def test_a_stray_source_file_does_not_pull_in_a_toolchain() -> None:
    assert detect_languages(["main.go", "src/a.py"]) == frozenset()


def test_commands_run_cheapest_first() -> None:
    commands = plan_gate(GateTier.QUICK, [Language.PYTHON, Language.RUST])
    kinds = [KIND_ORDER.index(command.kind) for command in commands]
    assert kinds == sorted(kinds)


def test_quick_tier_never_runs_yocto() -> None:
    commands = plan_gate(GateTier.QUICK, [Language.YOCTO, Language.PYTHON])
    assert all(command.language is not Language.YOCTO for command in commands)


def test_full_tier_runs_yocto_and_sanitizers() -> None:
    names = {
        command.name for command in plan_gate(GateTier.FULL, [Language.YOCTO, Language.C_CPP])
    }
    assert {"bitbake", "ptest", "ctest-asan"} <= names


def test_docs_build_runs_in_quick_only_when_docs_changed() -> None:
    languages = [Language.PYTHON, Language.DOCS]
    untouched = plan_gate(GateTier.QUICK, languages, ["src/a.py"])
    touched = plan_gate(GateTier.QUICK, languages, ["docs/index.md"])
    assert "mkdocs-strict" not in {command.name for command in untouched}
    assert "mkdocs-strict" in {command.name for command in touched}


def test_docs_build_runs_when_nothing_is_known_about_changes() -> None:
    commands = plan_gate(GateTier.QUICK, [Language.DOCS])
    assert "mkdocs-strict" in {command.name for command in commands}


def test_planning_is_deterministic() -> None:
    first = plan_gate(GateTier.FULL, [Language.RUST, Language.PYTHON, Language.DOCS])
    second = plan_gate(GateTier.FULL, [Language.DOCS, Language.PYTHON, Language.RUST])
    assert first == second


def test_every_language_has_a_marker_and_a_command() -> None:
    for language, gate in GATE_MATRIX.items():
        assert gate.markers, language
        assert gate.commands, language


def test_empty_gate_does_not_pass() -> None:
    assert not GateResult(tier=GateTier.QUICK, steps=()).passed


def test_result_is_red_when_any_step_is_red() -> None:
    result = GateResult(
        tier=GateTier.QUICK,
        steps=(
            GateStepResult(command="ruff", exit_code=0, duration_seconds=0.2),
            GateStepResult(
                command="pytest", exit_code=1, duration_seconds=9.0, output="1 failed"
            ),
        ),
    )
    assert not result.passed
    assert [step.command for step in result.failures] == ["pytest"]


def test_failure_signature_ignores_paths_numbers_and_durations() -> None:
    first = GateResult(
        tier=GateTier.QUICK,
        steps=(
            GateStepResult(
                command="pytest",
                exit_code=1,
                duration_seconds=9.0,
                output="/home/a/repo/tests/test_x.py:41: error: bad thing in 9.1s",
            ),
        ),
    )
    second = GateResult(
        tier=GateTier.QUICK,
        steps=(
            GateStepResult(
                command="pytest",
                exit_code=1,
                duration_seconds=12.0,
                output="/srv/build/other/tests/test_x.py:77: error: bad thing in 12.4s",
            ),
        ),
    )
    assert first.failure_signature() == second.failure_signature()


def test_a_different_failure_gets_a_different_signature() -> None:
    def result(output: str) -> GateResult:
        return GateResult(
            tier=GateTier.QUICK,
            steps=(
                GateStepResult(
                    command="mypy", exit_code=1, duration_seconds=1.0, output=output
                ),
            ),
        )

    assert (
        result("error: missing return").failure_signature()
        != result(
            "error: incompatible type",
        ).failure_signature()
    )


def test_green_result_has_no_signature() -> None:
    result = GateResult(
        tier=GateTier.QUICK,
        steps=(GateStepResult(command="ruff", exit_code=0, duration_seconds=0.1),),
    )
    assert result.passed
    assert result.failure_signature() == ""


def test_normalisation_keeps_only_diagnostic_lines() -> None:
    output = "collecting ...\nall good so far\nerror: boom at 0x7ffd\n"
    assert normalize_output(output) == "error: boom at <addr>"


# ------------------------------------- a check this project does not configure

PYPROJECT = """
[project]
name = "demo"

[tool.ruff]
line-length = 96

[[tool.importlinter.contracts]]
name = "layers"
"""


def names(commands: object) -> list[str]:
    assert isinstance(commands, tuple)
    return [command.name for command in commands]


def test_a_check_this_project_does_not_configure_is_not_in_its_gate() -> None:
    # import-linter and REUSE are this repository's conventions, not Python's.
    # In a project that configures neither they are not a gate, they are two
    # red commands and a model with no way to make them green.
    listing = gate_listing(["pyproject.toml", "src/app.py", "tests/test_app.py"])
    planned = names(plan_gate(GateTier.QUICK, [Language.PYTHON], (), listing))
    assert "ruff" in planned
    assert "pytest" in planned
    assert "lint-imports" not in planned
    assert "reuse" not in planned


def test_a_project_that_does_configure_them_still_gets_them() -> None:
    listing = gate_listing(["pyproject.toml", "REUSE.toml", "src/app.py"], PYPROJECT)
    planned = names(plan_gate(GateTier.QUICK, [Language.PYTHON], (), listing))
    assert "lint-imports" in planned, "the pyproject declares [[tool.importlinter.contracts]]"
    assert "reuse" in planned


def test_a_requirement_is_met_by_a_directory_in_the_listing() -> None:
    listing = gate_listing(["pyproject.toml", "LICENSES/GPL-3.0-or-later.txt"])
    assert "reuse" in names(plan_gate(GateTier.QUICK, [Language.PYTHON], (), listing))


def test_without_a_listing_nothing_is_filtered() -> None:
    # An empty listing is not evidence of an empty worktree: a caller that
    # does not know must get the whole gate, not a quietly smaller one.
    planned = names(plan_gate(GateTier.QUICK, [Language.PYTHON]))
    assert "lint-imports" in planned
    assert "reuse" in planned


def test_the_listing_names_the_tables_a_pyproject_declares() -> None:
    listing = gate_listing(["pyproject.toml"], PYPROJECT)
    assert "pyproject.toml:tool.ruff" in listing
    assert "pyproject.toml:tool.importlinter" in listing, "[[tool.x.y]] counts as tool.x"
    assert "pyproject.toml:tool.mypy" not in listing
    assert gate_listing([], "") == frozenset()


# -------------------------------------------- a formatter that can also write


def test_every_formatter_in_the_matrix_can_write() -> None:
    # A format command that can only complain is a gate a model cannot pass:
    # it would have to guess the formatter's output in a hand-written diff.
    for gate in GATE_MATRIX.values():
        for command in gate.commands:
            if command.kind is GateKind.FORMAT:
                assert command.apply_argv, f"{command.name} cannot write"
                assert command.apply_argv != command.argv, f"{command.name} would only check"


def test_the_formatters_offered_are_the_ones_the_project_configures() -> None:
    chosen = format_commands([Language.PYTHON, Language.RUST])
    assert [command.name for command in chosen] == ["ruff-format", "cargo-fmt"]
    assert format_commands([]) == ()


def test_a_formatter_for_a_check_the_project_does_not_configure_is_left_out() -> None:
    # The same requirement rule as the gate: nothing is offered for a
    # convention this project has not adopted.
    docs_only = format_commands([Language.DOCS], ["mkdocs.yml"])
    assert docs_only == (), "there is no formatter for prose in the matrix"

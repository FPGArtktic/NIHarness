# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration layers: defaults, profile, project file, environment."""

from pathlib import Path

import pytest

from niharness.config import HarnessConfig


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "cpu-only.toml").write_text(
        "\n".join(
            [
                "[compact]",
                "context_window = 32768",
                "threshold = 0.6",
                "[models.main]",
                'name = "Qwen3-Coder-30B-A3B-Instruct"',
                "context_window = 32768",
                "[sandbox]",
                "cpus = 8.0",
            ],
        ),
        encoding="utf-8",
    )
    (tmp_path / "harness.toml").write_text(
        "\n".join(
            [
                'profile = "cpu-only"',
                "max_revisions = 5",
                "[compact]",
                "threshold = 0.8",
                "[loop]",
                "progress_stall_calls = 12",
            ],
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_defaults_apply_without_any_file(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    assert config.profile == "hybrid-small-vram"
    assert config.compact.threshold == 0.75
    assert config.sandbox.network == "none"


def test_the_project_file_overrides_the_profile(project: Path) -> None:
    config = HarnessConfig.load(project)
    assert config.profile == "cpu-only"
    assert config.max_revisions == 5
    assert config.compact.threshold == 0.8
    assert config.compact.context_window == 32_768
    assert config.sandbox.cpus == 8.0
    assert config.loop.progress_stall_calls == 12


def test_an_explicit_profile_wins_over_the_project_file(project: Path) -> None:
    config = HarnessConfig.load(project, profile="missing-profile")
    assert config.profile == "missing-profile"
    assert config.compact.context_window == 65_536


def test_overrides_are_applied_last(project: Path) -> None:
    config = HarnessConfig.load(project, overrides={"max_revisions": 0})
    assert config.max_revisions == 0


def test_environment_variables_win(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIH_MAX_REVISIONS", "9")
    assert HarnessConfig.load(project).max_revisions == 9


def test_a_nested_environment_override_keeps_the_rest_of_the_section(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIH_COMPACT__THRESHOLD", "0.5")
    config = HarnessConfig.load(project)
    assert config.compact.threshold == 0.5
    assert config.compact.context_window == 32_768


def test_paths_are_derived_from_the_project_root(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    assert config.state_dir == tmp_path.resolve() / ".nih"
    assert config.runs_dir == tmp_path.resolve() / ".nih" / "runs"
    assert config.worktrees_dir.name == "worktrees"
    assert config.approved_plan_path.name == "approved-plan.md"


def test_the_configuration_is_frozen(tmp_path: Path) -> None:
    config = HarnessConfig.load(tmp_path)
    with pytest.raises(ValueError, match="frozen"):
        config.max_revisions = 1  # type: ignore[misc]  # the config is frozen


def test_the_sandbox_cannot_be_given_a_network(tmp_path: Path) -> None:
    (tmp_path / "harness.toml").write_text('[sandbox]\nnetwork = "host"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="network"):
        HarnessConfig.load(tmp_path)


def test_an_unknown_key_is_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "harness.toml").write_text("[compact]\nwindow = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="window"):
        HarnessConfig.load(tmp_path)

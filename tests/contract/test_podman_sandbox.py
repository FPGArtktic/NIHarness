# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The sandbox adapter, with the container replaced by a recording runner.

The hardening flags are the whole point of this adapter, so they are asserted
here rather than trusted. No Podman is involved: these tests must pass on a
machine that does not have it.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest

from niharness.adapters.podman import (
    PodmanSandbox,
    SandboxImageMissingError,
    SandboxUnavailableError,
)
from niharness.adapters.podman.sandbox import (
    GRACE_SECONDS,
    TIMEOUT_EXIT_CODE,
    CompletedProcess,
    subprocess_runner,
)
from niharness.domain.tools import PathConfinementError

IMAGE = "localhost/niharness-sandbox:latest"


class Recorder:
    """A runner that records what it was asked to run and replays canned results."""

    def __init__(self, *results: CompletedProcess) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self._results = list(results) or [CompletedProcess(0, "", "", False)]

    async def __call__(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
    ) -> CompletedProcess:
        self.calls.append((tuple(argv), timeout_seconds))
        return self._results[min(len(self.calls) - 1, len(self._results) - 1)]


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


def sandbox(worktree: Path, runner: Recorder | None = None) -> PodmanSandbox:
    return PodmanSandbox(IMAGE, worktree, runner=runner or Recorder())


def test_the_container_has_no_network_and_cannot_gain_privileges(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(["pytest"])
    assert "--network=none" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_only_the_worktree_is_writable(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(["pytest"])
    assert "--read-only" in argv
    assert f"--volume={worktree}:/work:rw,Z" in argv
    assert sum(1 for item in argv if item.startswith("--volume=")) == 1


def test_resources_are_capped(worktree: Path) -> None:
    runner = Recorder()
    box = PodmanSandbox(IMAGE, worktree, cpus=2.5, memory="4g", pids_limit=64, runner=runner)
    argv = box.container_argv(["cargo", "test"], timeout_seconds=300)
    assert "--cpus=2.5" in argv
    assert "--memory=4g" in argv
    assert "--pids-limit=64" in argv
    assert "--timeout=300" in argv


def test_the_command_follows_the_image(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(["ruff", "check", "."])
    assert argv[0] == "podman"
    assert argv[1] == "run"
    assert argv[-4:] == (IMAGE, "ruff", "check", ".")


def test_the_working_directory_is_confined(worktree: Path) -> None:
    box = sandbox(worktree)
    assert "--workdir=/work/src/niharness" in box.container_argv(
        ["ruff", "check", "."],
        cwd="src/niharness",
    )
    assert "--workdir=/work" in box.container_argv(["ruff", "check", "."])
    assert "--workdir=/work/src" in box.container_argv(["ls"], cwd="build/../src")


@pytest.mark.parametrize("cwd", ["..", "../../etc", "/etc", "~/.ssh"])
def test_a_working_directory_that_escapes_is_refused(worktree: Path, cwd: str) -> None:
    with pytest.raises(PathConfinementError):
        sandbox(worktree).container_argv(["ls"], cwd=cwd)


def test_an_empty_command_is_refused(worktree: Path) -> None:
    with pytest.raises(ValueError, match="empty command"):
        sandbox(worktree).container_argv([])


def test_the_host_environment_is_not_inherited(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(["pytest"])
    assert not [item for item in argv if item.startswith("--env=")]


def test_requested_environment_is_passed_in_a_stable_order(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(
        ["bitbake", "core-image-minimal"],
        env={"DL_DIR": "/work/.cache/dl", "BB_NO_NETWORK": "1"},
    )
    passed = [item for item in argv if item.startswith("--env=")]
    assert passed == ["--env=BB_NO_NETWORK=1", "--env=DL_DIR=/work/.cache/dl"]


@pytest.mark.parametrize("name", ["path", "PATH-EXTRA", "1PATH", "", "LD_PRELOAD;x"])
def test_a_suspect_environment_name_is_refused(worktree: Path, name: str) -> None:
    with pytest.raises(ValueError, match="environment variable name"):
        sandbox(worktree).container_argv(["pytest"], env={name: "x"})


def test_the_adapter_reports_the_air_gap(worktree: Path) -> None:
    assert sandbox(worktree).network_disabled is True


async def test_ensure_ready_accepts_a_present_image(worktree: Path) -> None:
    runner = Recorder(
        CompletedProcess(0, "5.2.0\n", "", False),
        CompletedProcess(0, "", "", False),
    )
    await sandbox(worktree, runner).ensure_ready()
    assert runner.calls[0][0][:2] == ("podman", "version")
    assert runner.calls[1][0] == ("podman", "image", "exists", IMAGE)


async def test_ensure_ready_reports_a_broken_podman(worktree: Path) -> None:
    runner = Recorder(CompletedProcess(127, "", "command not found", False))
    with pytest.raises(SandboxUnavailableError, match="command not found"):
        await sandbox(worktree, runner).ensure_ready()


async def test_a_missing_image_is_never_pulled(worktree: Path) -> None:
    runner = Recorder(
        CompletedProcess(0, "5.2.0\n", "", False),
        CompletedProcess(1, "", "", False),
    )
    with pytest.raises(SandboxImageMissingError) as error:
        await sandbox(worktree, runner).ensure_ready()
    assert "podman build" in str(error.value)
    assert not [call for call in runner.calls if "pull" in call[0]]


async def test_run_reports_the_requested_command_not_the_podman_one(worktree: Path) -> None:
    runner = Recorder(CompletedProcess(0, "42 passed", "", False))
    result = await sandbox(worktree, runner).run(["pytest", "-q"])
    assert result.argv == ("pytest", "-q")
    assert result.ok
    assert result.output.startswith("42 passed")
    assert result.duration_seconds >= 0


async def test_run_keeps_a_red_exit_code_red(worktree: Path) -> None:
    runner = Recorder(CompletedProcess(1, "", "1 failed", False))
    result = await sandbox(worktree, runner).run(["pytest"])
    assert not result.ok
    assert result.exit_code == 1
    assert "1 failed" in result.output


async def test_podman_gets_a_grace_period_past_the_container_timeout(worktree: Path) -> None:
    runner = Recorder()
    await sandbox(worktree, runner).run(["sleep", "1"], timeout_seconds=30)
    argv, timeout = runner.calls[0]
    assert "--timeout=30" in argv
    assert timeout == pytest.approx(30 + GRACE_SECONDS)


async def test_a_timeout_is_reported_rather_than_mistaken_for_success(worktree: Path) -> None:
    runner = Recorder(CompletedProcess(TIMEOUT_EXIT_CODE, "", "killed", True))
    result = await sandbox(worktree, runner).run(["cargo", "build"], timeout_seconds=1)
    assert result.timed_out
    assert not result.ok


async def test_the_default_runner_executes_a_local_command() -> None:
    completed = await subprocess_runner(["/bin/echo", "hello"], 10.0)
    assert completed.exit_code == 0
    assert completed.stdout.strip() == "hello"
    assert not completed.timed_out


async def test_the_default_runner_reports_a_missing_binary() -> None:
    with pytest.raises(SandboxUnavailableError, match="cannot execute"):
        await subprocess_runner(["/nonexistent/podman", "version"], 10.0)


def test_the_tool_server_keeps_standard_input_open(worktree: Path) -> None:
    argv = sandbox(worktree).tool_server_argv()
    assert "--interactive" in argv
    assert argv[-5:] == ("python", "-m", "niharness.toolserver", "--root", "/work")
    assert "--network=none" in argv, "the server is hardened like any other call"
    assert "--cap-drop=ALL" in argv
    assert not [item for item in argv if item.startswith("--timeout=")], (
        "a stdio session is long-lived; a container timeout would kill it mid-run"
    )


def test_an_ordinary_call_does_not_keep_standard_input_open(worktree: Path) -> None:
    argv = sandbox(worktree).container_argv(["pytest"])
    assert "--interactive" not in argv
    assert "--timeout=120" in argv

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The sandbox adapter against real Podman.

Skipped unless Podman is installed and `NIH_SANDBOX_IMAGE` names an image that
is already present locally - the harness never pulls one, and neither do its
tests. Run with:

    NIH_SANDBOX_IMAGE=localhost/niharness-sandbox:latest uv run pytest -m integration
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from niharness.adapters.podman import PodmanSandbox

IMAGE = os.environ.get("NIH_SANDBOX_IMAGE", "")


def _image_present(image: str) -> bool:
    if not image or shutil.which("podman") is None:
        return False
    return (
        subprocess.run(
            ["podman", "image", "exists", image],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _image_present(IMAGE),
        reason="set NIH_SANDBOX_IMAGE to a locally present image",
    ),
]


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    (path / "hello.txt").write_text("hello from the worktree\n", encoding="utf-8")
    return path


@pytest.fixture
def sandbox(worktree: Path) -> PodmanSandbox:
    return PodmanSandbox(IMAGE, worktree, cpus=1.0, memory="1g")


async def test_the_image_and_podman_are_usable(sandbox: PodmanSandbox) -> None:
    await sandbox.ensure_ready()


async def test_a_command_sees_the_worktree(sandbox: PodmanSandbox) -> None:
    result = await sandbox.run(["cat", "hello.txt"])
    assert result.ok
    assert "hello from the worktree" in result.stdout


async def test_the_worktree_is_writable_and_the_host_sees_the_write(
    sandbox: PodmanSandbox,
    worktree: Path,
) -> None:
    result = await sandbox.run(["sh", "-c", "echo written > out.txt"])
    assert result.ok, result.output
    assert (worktree / "out.txt").read_text(encoding="utf-8").strip() == "written"


async def test_the_root_filesystem_is_read_only(sandbox: PodmanSandbox) -> None:
    result = await sandbox.run(["sh", "-c", "echo x > /etc/nih-probe"])
    assert not result.ok


async def test_there_is_no_network(sandbox: PodmanSandbox) -> None:
    result = await sandbox.run(["sh", "-c", "cat /proc/net/route | tail -n +2"])
    assert result.stdout.strip() == "", "the sandbox must have no routes"


async def test_a_red_command_stays_red(sandbox: PodmanSandbox) -> None:
    result = await sandbox.run(["false"])
    assert not result.ok
    assert result.exit_code != 0


async def test_a_hanging_command_is_stopped(sandbox: PodmanSandbox) -> None:
    result = await sandbox.run(["sleep", "30"], timeout_seconds=2)
    assert not result.ok
    assert result.duration_seconds < 30

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Command execution in a rootless Podman container.

Models never get a host shell. Every command the harness runs on a model's
behalf runs here: one short-lived container per command, with no network
interface, no capabilities, a read-only root filesystem, and exactly one
writable path - the run's worktree, bind-mounted at `/work`.

The argument vector is built by :meth:`PodmanSandbox.container_argv`, which is
a pure function of the configuration, so the hardening flags are asserted by a
contract test rather than hoped for. Nothing in this adapter ever pulls an
image: on an air-gapped host there is nothing to pull from.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Protocol

import structlog

from niharness.domain.tools import confine_path
from niharness.ports.sandbox import ProcessResult

WORKDIR = PurePosixPath("/work")
"""Where the run's worktree is mounted inside the container."""

TIMEOUT_EXIT_CODE = 124
"""What `timeout(1)` returns, reused so a killed command is recognisable."""

GRACE_SECONDS = 15.0
"""How long the harness waits past the container timeout before killing Podman."""

ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
"""Environment variable names the sandbox accepts."""

_log = structlog.get_logger(__name__)


class SandboxUnavailableError(RuntimeError):
    """Podman is not installed, or cannot be executed."""


class SandboxImageMissingError(RuntimeError):
    """The sandbox image is not present locally, and the harness never pulls."""

    def __init__(self, image: str) -> None:
        self.image = image
        super().__init__(
            f"sandbox image {image!r} is not present locally. Build it on a machine "
            "with network access and load it here:\n"
            "  podman build -t "
            f"{image} -f containers/Containerfile.sandbox .\n"
            "  podman save <image> | podman load   (on the air-gapped host)",
        )


class CompletedProcess(NamedTuple):
    """What a runner reports back about one Podman invocation."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class ProcessRunner(Protocol):
    """Executes an argument vector. Injected so contract tests need no container."""

    async def __call__(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
    ) -> CompletedProcess:
        """Run `argv` and return its outcome."""
        ...


async def subprocess_runner(
    argv: Sequence[str],
    timeout_seconds: float,
) -> CompletedProcess:
    """Run `argv` as a child process.

    Podman is given its own `--timeout`, so it normally stops the container and
    returns output of its own accord. The timeout here is the backstop for
    Podman itself hanging, and it loses the output, which is why it is longer.

    Args:
        argv: the full Podman command.
        timeout_seconds: when to kill Podman itself.

    Returns:
        The exit code, output and whether the backstop fired.

    Raises:
        SandboxUnavailableError: if the Podman binary cannot be executed.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise SandboxUnavailableError(f"cannot execute {argv[0]!r}: {error}") from error
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        return CompletedProcess(
            exit_code=TIMEOUT_EXIT_CODE,
            stdout="",
            stderr=f"podman did not return within {timeout_seconds:.0f}s and was killed",
            timed_out=True,
        )
    return CompletedProcess(
        exit_code=process.returncode if process.returncode is not None else TIMEOUT_EXIT_CODE,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        timed_out=False,
    )


class PodmanSandbox:
    """A :class:`niharness.ports.sandbox.Sandbox` over rootless Podman."""

    def __init__(
        self,
        image: str,
        worktree: Path,
        *,
        cpus: float = 4.0,
        memory: str = "8g",
        pids_limit: int = 512,
        binary: str = "podman",
        runner: ProcessRunner | None = None,
    ) -> None:
        """Create a sandbox for one run's worktree.

        Args:
            image: the local image to run; it is never pulled.
            worktree: the only writable path the container gets.
            cpus: CPU quota, so a runaway test suite cannot take the machine.
            memory: memory cap, same reason.
            pids_limit: process cap, against fork bombs in a build script.
            binary: the Podman executable.
            runner: injected executor; contract tests replace it.
        """
        self._image = image
        self._worktree = worktree
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit
        self._binary = binary
        self._runner: ProcessRunner = runner or subprocess_runner

    @property
    def network_disabled(self) -> bool:
        """Always true: `--network=none` is not configurable."""
        return True

    def container_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: Mapping[str, str] | None = None,
        interactive: bool = False,
    ) -> tuple[str, ...]:
        """Return the full Podman command for `argv`.

        Args:
            argv: the command to run inside the container.
            cwd: working directory, relative to the worktree.
            timeout_seconds: container-level timeout handed to Podman.
            env: environment variables to set inside the container. The host
                environment is not inherited: only these reach the command.
            interactive: keep standard input open. The tool server speaks the
                protocol over stdio, so it needs this; a gate command does not,
                and leaving it closed is one less thing a command can wait on.

        Returns:
            The complete argument vector, hardening flags included.

        Raises:
            ValueError: if `argv` is empty, or an environment variable name is
                not a plain uppercase identifier.
            niharness.domain.tools.PathConfinementError: if `cwd` escapes the
                worktree.
        """
        if not argv:
            msg = "refusing to run an empty command"
            raise ValueError(msg)
        inside = WORKDIR / confine_path(cwd)
        command: list[str] = [
            self._binary,
            "run",
            "--rm",
            *(["--interactive"] if interactive else []),
            # The air gap, enforced by the kernel rather than by convention.
            "--network=none",
            # Only the worktree is writable; everything else is read-only.
            "--read-only",
            "--read-only-tmpfs",
            f"--volume={self._worktree}:{WORKDIR}:rw,Z",
            f"--workdir={inside}",
            # Keep the caller's uid inside, so files in the worktree stay theirs.
            "--userns=keep-id",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--cpus={self._cpus}",
            f"--memory={self._memory}",
            f"--pids-limit={self._pids_limit}",
            *([] if interactive else [f"--timeout={timeout_seconds}"]),
            "--label=nih.sandbox=1",
        ]
        for name in sorted(env or {}):
            if not ENV_NAME.match(name):
                msg = f"refusing environment variable name {name!r}"
                raise ValueError(msg)
            command.append(f"--env={name}={(env or {})[name]}")
        command.append(self._image)
        command.extend(argv)
        return tuple(command)

    async def ensure_ready(self) -> None:
        """Check that Podman works and the image is present locally.

        Raises:
            SandboxUnavailableError: if Podman cannot be executed.
            SandboxImageMissingError: if the image is absent. The harness does
                not pull it; an air-gapped host has nowhere to pull from.
        """
        version = await self._runner(
            (self._binary, "version", "--format={{.Client.Version}}"), 30.0
        )
        if version.exit_code != 0:
            detail = version.stderr.strip() or version.stdout.strip()
            raise SandboxUnavailableError(f"{self._binary} version failed: {detail}")
        exists = await self._runner((self._binary, "image", "exists", self._image), 30.0)
        if exists.exit_code != 0:
            raise SandboxImageMissingError(self._image)
        _log.info(
            "sandbox.ready",
            image=self._image,
            podman=version.stdout.strip(),
            worktree=str(self._worktree),
        )

    def tool_server_argv(self, root: str = "/work") -> tuple[str, ...]:
        """Return the command that starts this project's tool server in the sandbox.

        Args:
            root: where the worktree is mounted inside the container.

        Returns:
            A Podman command with standard input open and every hardening flag
            of an ordinary call. This is the one long-lived container: a stdio
            session has state.
        """
        return self.container_argv(
            ("python", "-m", "niharness.toolserver", "--root", root),
            interactive=True,
        )

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Run `argv` inside the sandbox and return its result."""
        command = self.container_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        started = time.monotonic()
        completed = await self._runner(command, timeout_seconds + GRACE_SECONDS)
        duration = time.monotonic() - started
        result = ProcessResult(
            argv=tuple(argv),
            exit_code=completed.exit_code,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration,
            timed_out=completed.timed_out,
        )
        _log.debug(
            "sandbox.ran",
            command=argv[0],
            exit_code=result.exit_code,
            seconds=round(duration, 3),
            timed_out=result.timed_out,
        )
        return result

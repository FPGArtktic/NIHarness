# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The sandbox port.

Models never get a host shell. Every command the harness runs on behalf of a
model runs in a rootless container with `--network=none` and the project
directory bind-mounted. That flag is not optional.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ProcessResult(BaseModel):
    """The outcome of one command executed in the sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False

    @property
    def output(self) -> str:
        """Interleaved output, as the gate reads it."""
        return self.stdout if not self.stderr else f"{self.stdout}\n{self.stderr}"

    @property
    def ok(self) -> bool:
        """True when the command exited zero and did not time out."""
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class Sandbox(Protocol):
    """Runs commands in an isolated, network-less container."""

    async def ensure_ready(self) -> None:
        """Check the image exists and a container can start. Never pulls from a network."""
        ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: int = 120,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """Run `argv` inside the sandbox, with `cwd` relative to the worktree."""
        ...

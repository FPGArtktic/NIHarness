# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The run registry: what exists, what is alive, and how to steer it."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from niharness.adapters.jsonl_control import JsonlControlChannel
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.config import HarnessConfig
from niharness.domain.artifacts import Answer, Artifact, Plan
from niharness.domain.control import AnswerQuestions, CancelRun, ControlMessage, Driver
from niharness.domain.events import Event, RunOutcome
from niharness.domain.identity import RunId, digest_text
from niharness.domain.state import RunSnapshot, fold


def seeded_run_id(seed: str, now: datetime | None = None) -> RunId:
    """Return a run id for a stage seeded by text rather than by an artifact.

    Brainstorming is the one stage with nothing to start from: a human
    sentence seeds it, so the id is derived from that sentence exactly as
    every other id is derived from its artifact's digest.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return RunId(f"{stamp}-{str(digest_text(seed))[:6]}")


def new_run_id(artifact: Artifact, now: datetime | None = None) -> RunId:
    """Return a readable, unique run id: when it started and what seeded it.

    Any artifact will do, because a stage boundary starts a fresh run seeded
    by exactly one of them: a plan for an implementing run, a spec for a
    planning run. The id says which, by digest, so two runs over the same
    input are told apart by their timestamp and not by luck.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return RunId(f"{stamp}-{str(artifact.digest())[:6]}")


PID_FILE = "run.pid"
LOG_FILE = "implement.log"

_log = structlog.get_logger(__name__)


class RunEntry(BaseModel):
    """One row of the registry: what a run is, folded from its log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    live: bool
    stage: str
    outcome: str
    events: int
    tool_calls: int
    commits: int
    context_fill: float


class NoSuchRunError(LookupError):
    """The run id does not name a run of this project."""


class NotResumableError(RuntimeError):
    """The run cannot be continued, and the reason says what to do instead."""


class ResumePoint(BaseModel):
    """Where a run stopped, and what a continuation may skip."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    from_seq: int
    done: tuple[str, ...]
    plan_digest: str


class RunRegistry:
    """Reads and steers the runs under one project's state directory."""

    def __init__(self, config: HarnessConfig) -> None:
        """Bind to a project."""
        self._config = config

    @property
    def runs_dir(self) -> Path:
        """Where run state lives."""
        return self._config.runs_dir

    def ids(self) -> tuple[RunId, ...]:
        """Return the run ids that have an event log."""
        if not self.runs_dir.is_dir():
            return ()
        return tuple(
            RunId(entry.name)
            for entry in sorted(self.runs_dir.iterdir())
            if (entry / "events.jsonl").is_file()
        )

    def require(self, run_id: str) -> RunId:
        """Return `run_id` if it names a run.

        Raises:
            NoSuchRunError: when it does not.
        """
        if RunId(run_id) not in self.ids():
            raise NoSuchRunError(run_id)
        return RunId(run_id)

    def is_live(self, run_id: RunId) -> bool:
        """True when a process still owns this run.

        The pid file is written while `nih implement` runs; a file left behind
        by a crash is caught by asking the kernel instead of trusting it.
        """
        pid_path = self.runs_dir / run_id / PID_FILE
        if not pid_path.is_file():
            return False
        text = pid_path.read_text(encoding="utf-8").strip()
        if not text.isdigit():
            return False
        try:
            os.kill(int(text), 0)
        except OSError:
            return False
        return True

    async def events(self, run_id: RunId) -> tuple[Event, ...]:
        """Return a run's events, in order."""
        sink = JsonlEventSink(self.runs_dir, run_id)
        return tuple([event async for event in sink.replay(run_id)])

    async def snapshot(self, run_id: RunId) -> RunSnapshot:
        """Fold a run's event log."""
        return fold(list(await self.events(run_id)), run_id)

    async def entry(self, run_id: RunId) -> RunEntry:
        """Return one registry row."""
        snapshot = await self.snapshot(run_id)
        return RunEntry(
            run_id=run_id,
            live=self.is_live(run_id),
            stage=snapshot.stage or "-",
            outcome=snapshot.outcome.value if snapshot.outcome else "running",
            events=snapshot.seq,
            tool_calls=snapshot.tool_calls,
            commits=len(snapshot.commits),
            context_fill=snapshot.context_fill,
        )

    async def collect(self) -> tuple[RunEntry, ...]:
        """Return every run as a registry row."""
        return tuple([await self.entry(run_id) for run_id in self.ids()])

    async def resume_point(self, run_id: RunId) -> ResumePoint:
        """Return where `run_id` stopped.

        Raises:
            NotResumableError: when the run is still going, or finished, or
                never recorded which plan it was running. A resume that guessed
                at any of those would be worse than a refusal.
        """
        if self.is_live(run_id):
            raise NotResumableError(f"{run_id} is still running; cancel it first")
        snapshot = await self.snapshot(run_id)
        if snapshot.outcome is RunOutcome.COMPLETED:
            raise NotResumableError(f"{run_id} completed; there is nothing to continue")
        if snapshot.plan_digest is None:
            raise NotResumableError(f"{run_id} never recorded which plan it was running")
        return ResumePoint(
            run_id=run_id,
            from_seq=snapshot.seq,
            done=tuple(task.task_id for task in snapshot.tasks if task.done),
            plan_digest=str(snapshot.plan_digest),
        )

    def worktree_path(self, run_id: RunId) -> Path:
        """Where this run's worktree is."""
        return self._config.worktrees_dir / run_id

    # ------------------------------------------------------------- steering

    async def cancel(self, run_id: RunId, reason: str, source: Driver) -> ControlMessage:
        """Ask a run to stop at its next step boundary."""
        channel = JsonlControlChannel(self.runs_dir, run_id)
        return await channel.submit(CancelRun(run_id=run_id, reason=reason, source=source))

    async def answer(
        self,
        run_id: RunId,
        answers: Sequence[Answer],
        source: Driver,
    ) -> ControlMessage:
        """Answer the questions a run is waiting on."""
        channel = JsonlControlChannel(self.runs_dir, run_id)
        return await channel.submit(
            AnswerQuestions(run_id=run_id, answers=tuple(answers), source=source),
        )

    def start_detached(
        self,
        run_id: RunId,
        plan: Plan,
        *,
        plan_file: Path | None = None,
        local_tools: bool = False,
        resume: bool = False,
    ) -> RunId:
        """Start a run in its own process and return the run id.

        The child is invoked as `python -m niharness`: the interpreter's path is
        something this process can be certain of, where the location of a
        console script depends on how the environment was installed.
        """
        del plan
        log_dir = self.runs_dir / run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "niharness",
            *(["resume", run_id] if resume else ["implement"]),
            "-C",
            str(self._config.project_root),
        ]
        if not resume:
            command += ["--run-id", run_id]
        if plan_file is not None and not resume:
            command += ["--plan", str(plan_file)]
        if local_tools:
            command.append("--unsafe-local-tools")
        with (log_dir / LOG_FILE).open("ab") as log:
            subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter, no shell
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(self._config.project_root),
            )
        _log.info("run.detached", run_id=run_id, log=str(log_dir / LOG_FILE))
        return run_id


@contextlib.contextmanager
def pid_file(runs_dir: Path, run_id: RunId) -> Iterator[None]:
    """Record this process as the owner of `run_id` while it runs."""
    path = runs_dir / run_id / PID_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()

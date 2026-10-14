# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Operations on one worktree, with the confinement that only a filesystem can do.

The domain already rejects absolute paths and lexical escapes before a call is
dispatched. This is the second check, and the one that matters against a
symlink: every path is resolved and must land inside the root. The container is
the third, and if the first two are ever wrong it is the one that holds.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path

import structlog

from niharness.domain.gate import (
    GateKind,
    GateResult,
    GateStepResult,
    GateTier,
    detect_languages,
    plan_gate,
)
from niharness.domain.tools import (
    PathConfinementError,
    ToolLimits,
    confine_path,
    truncate_result,
    validate_commit_message,
)
from niharness.ports.sandbox import ProcessResult

CommandRunner = Callable[[Sequence[str], Path, int], Awaitable[ProcessResult]]
"""Runs an argv in a directory with a timeout. Injected so tests need no toolchain."""

SKIP_DIRECTORIES: frozenset[str] = frozenset({".git", ".nih", "__pycache__", "node_modules"})
"""Directories no listing or search descends into."""

MAX_SEARCH_BYTES = 2_000_000
"""Files larger than this are not searched; they are not source."""

_log = structlog.get_logger(__name__)


class WorkspaceError(RuntimeError):
    """A tool could not do what it was asked, for a reason the model should read."""


async def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: int) -> ProcessResult:
    """Run `argv` in `cwd`. Inside the sandbox this is already the isolated side."""
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise WorkspaceError(f"cannot run {argv[0]!r}: {error}") from error
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ProcessResult(
            argv=tuple(argv),
            exit_code=124,
            stderr=f"{argv[0]} exceeded {timeout}s and was killed",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    return ProcessResult(
        argv=tuple(argv),
        exit_code=process.returncode if process.returncode is not None else 1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
        duration_seconds=time.monotonic() - started,
    )


class Workspace:
    """Everything the tools do, against one worktree."""

    def __init__(
        self,
        root: Path,
        *,
        runner: CommandRunner | None = None,
        limits: ToolLimits | None = None,
        command_timeout: int = 900,
    ) -> None:
        """Bind to a worktree.

        Args:
            root: the only directory the tools may read or write.
            runner: how commands are executed; the default runs them here.
            limits: caps on results, diffs and commit messages.
            command_timeout: seconds a gate command may take.
        """
        self._root = root.resolve()
        self._runner: CommandRunner = runner or subprocess_runner
        self._limits = limits or ToolLimits()
        self._timeout = command_timeout

    @property
    def root(self) -> Path:
        """The worktree this server is bound to."""
        return self._root

    def resolve(self, candidate: str) -> Path:
        """Return `candidate` as a real path inside the worktree.

        Args:
            candidate: a worktree-relative path as the model wrote it.

        Returns:
            The resolved path.

        Raises:
            WorkspaceError: if the path is absolute, escapes lexically, or
                resolves - through a symlink or otherwise - outside the root.
        """
        try:
            relative = confine_path(candidate)
        except PathConfinementError as error:
            raise WorkspaceError(str(error)) from error
        resolved = (self._root / relative).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise WorkspaceError(f"path_escape: {candidate!r} resolves outside the worktree")
        return resolved

    # ------------------------------------------------------------- read-only

    async def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> str:
        """Return a slice of one file, line-numbered and truncated to the cap."""
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"not a file: {path}")
        text = await asyncio.to_thread(target.read_text, "utf-8", "replace")
        lines = text.splitlines()
        last = min(
            end_line or len(lines), len(lines), start_line + self._limits.max_read_lines - 1
        )
        if start_line > len(lines):
            raise WorkspaceError(f"{path} has {len(lines)} lines; {start_line} is past the end")
        body = "\n".join(
            f"{number:>6}  {lines[number - 1]}" for number in range(start_line, last + 1)
        )
        content, _ = truncate_result(body, self._limits)
        return content

    async def list_tree(self, path: str = ".", depth: int = 2) -> str:
        """Return a bounded directory listing."""
        target = self.resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {path}")
        entries = await asyncio.to_thread(self._walk, target, depth)
        content, _ = truncate_result("\n".join(entries) or "(empty)", self._limits)
        return content

    def _walk(self, target: Path, depth: int) -> list[str]:
        """Collect a sorted listing to `depth` levels, skipping noise directories."""
        found: list[str] = []
        for current, directories, files in Path(target).walk():
            directories[:] = sorted(
                name for name in directories if name not in SKIP_DIRECTORIES
            )
            level = len(current.relative_to(target).parts)
            if level >= depth:
                directories[:] = []
            prefix = current.relative_to(self._root)
            for name in directories:
                found.append(f"{prefix / name}/")
            found.extend(str(prefix / name) for name in sorted(files))
        return sorted(found)

    async def grep(
        self,
        pattern: str,
        path: str = ".",
        glob: str = "",
        max_matches: int = 50,
    ) -> str:
        """Search the worktree for a regular expression."""
        try:
            expression = re.compile(pattern)
        except re.error as error:
            raise WorkspaceError(f"bad pattern: {error}") from error
        target = self.resolve(path)
        matches = await asyncio.to_thread(self._search, expression, target, glob, max_matches)
        if not matches:
            return f"no matches for {pattern!r}"
        content, _ = truncate_result("\n".join(matches), self._limits)
        return content

    def _search(
        self,
        expression: re.Pattern[str],
        target: Path,
        glob: str,
        max_matches: int,
    ) -> list[str]:
        """Scan files under `target`, newest-first order is not needed: paths sort."""
        found: list[str] = []
        candidates: Iterable[Path] = [target] if target.is_file() else sorted(target.rglob("*"))
        for candidate in candidates:
            if len(found) >= max_matches:
                break
            if not candidate.is_file() or set(candidate.parts) & SKIP_DIRECTORIES:
                continue
            if glob and not fnmatch.fnmatch(candidate.name, glob):
                continue
            if candidate.stat().st_size > MAX_SEARCH_BYTES:
                continue
            try:
                text = candidate.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            relative = candidate.relative_to(self._root)
            for number, line in enumerate(text.splitlines(), start=1):
                if expression.search(line):
                    found.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(found) >= max_matches:
                        break
        return found

    # ----------------------------------------------------------------- write

    async def apply_patch(self, diff: str) -> str:
        """Apply a unified diff. There is no write_file: every change is reviewable."""
        size = len(diff.encode("utf-8"))
        if size > self._limits.max_diff_bytes:
            raise WorkspaceError(
                f"diff is {size} bytes, limit is {self._limits.max_diff_bytes}"
            )
        check = await self._git(("apply", "--check", "-"), stdin=diff)
        if not check.ok:
            raise WorkspaceError(f"the diff does not apply:\n{check.output.strip()}")
        applied = await self._git(("apply", "-"), stdin=diff)
        if not applied.ok:
            raise WorkspaceError(f"applying the diff failed:\n{applied.output.strip()}")
        changed = await self._git(("status", "--porcelain"))
        return f"applied.\nworktree now:\n{changed.stdout.strip() or '(clean)'}"

    async def _git(self, args: Sequence[str], stdin: str | None = None) -> ProcessResult:
        """Run a git command in the worktree."""
        if stdin is None:
            return await self._runner(("git", *args), self._root, self._timeout)
        return await self._run_with_stdin(("git", *args), stdin)

    async def _run_with_stdin(self, argv: Sequence[str], stdin: str) -> ProcessResult:
        """Run a command that needs something on standard input."""
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self._root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate(stdin.encode("utf-8"))
        return ProcessResult(
            argv=tuple(argv),
            exit_code=process.returncode if process.returncode is not None else 1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            duration_seconds=time.monotonic() - started,
        )

    # ------------------------------------------------------------------ gate

    async def tracked_files(self) -> tuple[str, ...]:
        """Return the worktree's files, for language detection."""
        listing = await self._git(("ls-files", "--cached", "--others", "--exclude-standard"))
        return tuple(sorted(line for line in listing.stdout.splitlines() if line))

    async def run_gate(
        self,
        tier: GateTier = GateTier.QUICK,
        kinds: frozenset[GateKind] | None = None,
    ) -> GateResult:
        """Run the gate, or the part of it `kinds` selects."""
        languages = detect_languages(await self.tracked_files())
        commands = plan_gate(tier, languages, await self.changed_paths())
        steps: list[GateStepResult] = []
        for command in commands:
            if kinds is not None and command.kind not in kinds:
                continue
            result = await self._runner(command.argv, self._root, self._timeout)
            steps.append(
                GateStepResult(
                    command=command.name,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    output=result.output,
                ),
            )
            if not result.ok:
                break
        return GateResult(tier=tier, steps=tuple(steps))

    async def changed_paths(self) -> tuple[str, ...]:
        """Return what changed since HEAD."""
        status = await self._git(("status", "--porcelain=v1", "--untracked-files=all"))
        return tuple(
            sorted(line[3:].strip() for line in status.stdout.splitlines() if len(line) > 3)
        )

    def render_gate(self, result: GateResult) -> str:
        """Render a gate result for the model: the failure, not the whole log."""
        lines = [f"gate {result.tier.value}: {'green' if result.passed else 'RED'}"]
        for step in result.steps:
            lines.append(
                f"  {step.command}: exit {step.exit_code} in {step.duration_seconds:.1f}s"
            )
        for step in result.failures:
            lines += ["", f"--- {step.command} ---", step.output.strip()]
        content, _ = truncate_result("\n".join(lines), self._limits)
        return content

    async def git_commit(self, message: str) -> str:
        """Run the quick gate and commit only if it is green.

        Raises:
            WorkspaceError: if the message is not a Conventional Commits message
                with a body, if the gate is red, or if there is nothing to
                commit. A red gate is not an obstacle to route around: the
                caller gets the failing output and no commit.
        """
        try:
            subject = validate_commit_message(message, self._limits)
        except ValueError as error:
            raise WorkspaceError(str(error)) from error
        if not await self.changed_paths():
            raise WorkspaceError("nothing to commit; the worktree is clean")
        result = await self.run_gate(GateTier.QUICK)
        if not result.passed:
            _log.warning("gate.red", signature=result.failure_signature())
            raise WorkspaceError(
                "the gate is red, so there is no commit. Fix the cause, do not "
                f"weaken the check.\n\n{self.render_gate(result)}",
            )
        staged = await self._git(("add", "-A"))
        if not staged.ok:
            raise WorkspaceError(f"git add failed:\n{staged.output.strip()}")
        committed = await self._run_with_stdin(("git", "commit", "-F", "-"), message)
        if not committed.ok:
            raise WorkspaceError(f"git commit failed:\n{committed.output.strip()}")
        head = await self._git(("rev-parse", "--short", "HEAD"))
        _log.info("gate.green", subject=subject, sha=head.stdout.strip())
        return f"committed {head.stdout.strip()}: {subject}\n\n{self.render_gate(result)}"

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

from niharness.domain.diffs import (
    changes_nothing,
    ensure_terminated,
    is_not_a_diff,
    repair_new_file_headers,
    strip_line_numbers,
)
from niharness.domain.gate import (
    GateKind,
    GateResult,
    GateStepResult,
    GateTier,
    Language,
    detect_languages,
    format_commands,
    gate_listing,
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
from niharness.toolserver.lsp import LanguageServers, LspError

CommandRunner = Callable[[Sequence[str], Path, int], Awaitable[ProcessResult]]
"""Runs an argv in a directory with a timeout. Injected so tests need no toolchain."""

SKIP_DIRECTORIES: frozenset[str] = frozenset({".git", ".nih", "__pycache__", "node_modules"})
"""Directories no listing or search descends into."""

MAX_SEARCH_BYTES = 2_000_000
"""Files larger than this are not searched; they are not source."""

APPLY_VARIANTS: tuple[tuple[str, ...], ...] = (
    (),
    ("--recount",),
    ("-p0",),
    ("-p0", "--recount"),
)
"""How a hand-written diff is read, in order of how strictly it is taken.

`-p0` is for a diff whose paths carry no `a/` and `b/` prefix, which is what a
model writes when it has only seen the file's real path. `--recount` is for a
hunk header whose line counts do not match the hunk, which is what a model
writes when it has counted by eye. Neither changes what the diff may do.
"""

DIFF_LINE_PREFIXES: frozenset[str] = frozenset({" ", "+", "-", "\\"})
"""What a line inside a hunk may start with."""

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
        servers: LanguageServers | None = None,
    ) -> None:
        """Bind to a worktree.

        Args:
            root: the only directory the tools may read or write.
            runner: how commands are executed; the default runs them here.
            limits: caps on results, diffs and commit messages.
            command_timeout: seconds a gate command may take.
            servers: the language servers this worktree may reach; without them
                the three symbol tools say so instead of pretending.
        """
        self._root = root.resolve()
        self._runner: CommandRunner = runner or subprocess_runner
        self._limits = limits or ToolLimits()
        self._timeout = command_timeout
        self._servers = servers
        self._languages: frozenset[Language] | None = None

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

    # ------------------------------------------------------ language servers

    async def languages(self) -> frozenset[Language]:
        """Return the languages this worktree has, detected once.

        The detection is a `git ls-files` away and a run does not change which
        toolchains a project uses, so it is computed on first use and kept.
        """
        if self._languages is None:
            self._languages = detect_languages(await self.tracked_files())
        return self._languages

    def _ask(self, hint: str) -> LanguageServers:
        """Return the session, or refuse clearly.

        Raises:
            WorkspaceError: when this server was started without language
                servers, or the hint names a language that does not exist.
        """
        if self._servers is None:
            raise WorkspaceError(
                "language servers are not enabled for this run; use grep instead",
            )
        if hint and hint not in set(Language):
            known = ", ".join(sorted(language.value for language in Language))
            raise WorkspaceError(f"no such language {hint!r}; known: {known}")
        return self._servers

    async def _targets(self, hint: str) -> tuple[Language, ...]:
        """Return which languages to ask, narrowed by the hint when there is one."""
        detected = await self.languages()
        if hint:
            return (Language(hint),) if Language(hint) in detected else ()
        return tuple(sorted(detected))

    async def find_symbol(self, name: str, language: str = "") -> str:
        """Return where a symbol is defined, asking the language servers.

        Raises:
            WorkspaceError: when no server can be asked, or one failed in a way
                the model should hear about.
        """
        servers = self._ask(language)
        try:
            return await servers.find_symbol(name, await self._targets(language))
        except LspError as error:
            raise WorkspaceError(str(error)) from error

    async def find_references(
        self,
        name: str,
        max_results: int = 30,
        language: str = "",
    ) -> str:
        """Return who uses a symbol, asking the language servers.

        Raises:
            WorkspaceError: when no server can be asked, or one failed.
        """
        servers = self._ask(language)
        try:
            return await servers.find_references(
                name,
                await self._targets(language),
                max_results=max_results,
            )
        except LspError as error:
            raise WorkspaceError(str(error)) from error

    async def file_outline(self, path: str) -> str:
        """Return what is in a file, without reading the file.

        Raises:
            WorkspaceError: when the path escapes, is not there, or no server
                owns its suffix.
        """
        servers = self._ask("")
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path}: not a file")
        try:
            return await servers.outline(str(target.relative_to(self._root)))
        except LspError as error:
            raise WorkspaceError(str(error)) from error

    async def aclose(self) -> None:
        """Stop any language server this worktree started."""
        if self._servers is not None:
            await self._servers.aclose()

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
        """Apply a unified diff. There is no write_file: every change is reviewable.

        A model writes a diff by hand, and it gets two things wrong far more
        often than it gets the *change* wrong: the path prefixes (`a/` and
        `b/`, which `git apply` strips by default) and the line counts in the
        hunk header. Both are clerical, so each option set below is tried until
        one applies - the change still has to match the file's real content,
        which is the part that matters.

        Raises:
            WorkspaceError: when the diff is too large, or when no variant
                applies. The message then carries git's own output and, when
                git names a line, that line - because "corrupt patch at line 8"
                is not something a model can act on.
        """
        size = len(diff.encode("utf-8"))
        if size > self._limits.max_diff_bytes:
            raise WorkspaceError(
                f"diff is {size} bytes, limit is {self._limits.max_diff_bytes}"
            )
        # Three repairs, all clerical, all reported back - because a tool that
        # silently fixes its caller teaches it nothing. An unterminated last
        # line makes git call the whole patch corrupt at a line that looks
        # fine; a new file announced as `--- path` instead of `--- /dev/null`
        # is the mistake no git option forgives; and `read_file`'s own line
        # numbers, copied into the context, make every line mismatch.
        diff, terminated = ensure_terminated(diff)
        malformed = _malformed(diff)
        if malformed is not None:
            raise WorkspaceError(malformed)
        diff, created = repair_new_file_headers(diff, self._is_file)
        applied, failures = await self._apply_variants(diff)
        numbered = False
        if not applied:
            # Tried only after the diff as written has failed, and accepted
            # only because it applied. A wrong guess here is indistinguishable
            # from the diff that already did not apply.
            candidate, stripped = strip_line_numbers(diff)
            if stripped:
                applied, more = await self._apply_variants(candidate)
                failures += [] if applied else more
                if applied:
                    diff, numbered = candidate, True
        if not applied:
            # A patch that will not apply because it is already applied is not
            # a failure, and telling a model otherwise is how it gets stuck:
            # observed on a live run, the model re-sent the same patch for a
            # file it had already written, twice, until the detectors stopped
            # the step. git can tell the two apart - if the reverse applies,
            # the change is in the worktree, which is what was asked for.
            if await self._already_applied(diff):
                return (
                    "already applied: the worktree already contains this change, so "
                    "nothing was done. Read the file if you are unsure what is in it."
                )
            raise WorkspaceError(_apply_refusal(diff, failures))
        note = _repair_notes(terminated=terminated, created=created, numbered=numbered)
        changed = await self._git(("status", "--porcelain"))
        # The only writer in the harness is this method, which makes it the one
        # place a language server's view of the tree can be kept honest.
        if self._servers is not None:
            await self._servers.changed(_porcelain_paths(changed.stdout))
        return f"{note}applied.\nworktree now:\n{changed.stdout.strip() or '(clean)'}"

    async def _apply_variants(self, diff: str) -> tuple[bool, list[str]]:
        """Try each option set until one applies, and return what the rest said.

        Returns:
            Whether the diff was applied, and one failure line per variant that
            refused it - in variant order, so the most lenient one is last.
        """
        failures: list[str] = []
        for options in APPLY_VARIANTS:
            check = await self._git(("apply", "--check", *options, "-"), stdin=diff)
            if check.ok:
                applied = await self._git(("apply", *options, "-"), stdin=diff)
                if applied.ok:
                    return True, failures
                failures.append(_variant_failure(options, applied.output))
            else:
                failures.append(_variant_failure(options, check.output))
        return False, failures

    async def _already_applied(self, diff: str) -> bool:
        """Return whether this diff describes a change the worktree already has."""
        for options in APPLY_VARIANTS:
            reverse = await self._git(
                ("apply", "--reverse", "--check", *options, "-"),
                stdin=diff,
            )
            if reverse.ok:
                return True
        return False

    async def format_code(self, path: str = ".") -> str:
        """Run the project's formatters over the worktree.

        The gate's `format` commands ask whether the tree is formatted. These
        are the same tools in their writing form, which is the difference
        between a model that can satisfy the gate and one that has to guess a
        formatter's output in a hand-written diff.

        Raises:
            WorkspaceError: when no formatter is configured for this worktree,
                or one of them fails.
        """
        files = await self.tracked_files()
        commands = format_commands(
            detect_languages(files),
            gate_listing(files, self._pyproject()),
        )
        if not commands:
            raise WorkspaceError(
                "no formatter is configured for this worktree; "
                "write the diff in the style the gate expects",
            )
        note = "" if path == "." else f"(path {path!r} does not narrow the formatters yet)\n"
        lines: list[str] = []
        for command in commands:
            result = await self._runner(command.apply_argv, self._root, self._timeout)
            # A formatter says what it changed, which is what the model needs
            # to hear; there is no bookkeeping here to disagree with it.
            outcome = result.output.strip() or ("ok" if result.ok else "failed")
            lines.append(f"{command.name}: {outcome}")
            if not result.ok:
                raise WorkspaceError(f"{command.name} failed:\n{result.output.strip()}")
        changed = await self._git(("status", "--porcelain"))
        return (
            f"{note}" + "\n".join(lines) + f"\nworktree now:\n"
            f"{changed.stdout.strip() or '(clean)'}"
        )

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

    def _is_file(self, relative: str) -> bool:
        """Return whether a worktree-relative path is an existing file."""
        try:
            return self.resolve(relative).is_file()
        except WorkspaceError:
            return False

    def _pyproject(self) -> str:
        """Return this worktree's `pyproject.toml`, or nothing when there is none."""
        target = self._root / "pyproject.toml"
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

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
        files = await self.tracked_files()
        languages = detect_languages(files)
        # A command that checks a convention this project does not configure is
        # not part of its gate: see `gate_listing`.
        listing = gate_listing(files, self._pyproject())
        commands = plan_gate(tier, languages, await self.changed_paths(), listing)
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


def _porcelain_paths(output: str) -> tuple[str, ...]:
    """Return the paths in `git status --porcelain` output.

    A rename is reported as `old -> new`; the new name is the one a language
    server needs to hear about.
    """
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        candidate = line[3:].strip().strip('"')
        _, arrow, renamed = candidate.partition(" -> ")
        paths.append(renamed.strip('"') if arrow else candidate)
    return tuple(dict.fromkeys(paths))


def _variant_failure(options: Sequence[str], output: str) -> str:
    """Return one variant's refusal, labelled with the options it used."""
    label = " ".join(options) or "as written"
    return f"[{label}] {output.strip()}"


def _malformed(diff: str) -> str | None:
    """Return why a diff cannot be a patch at all, or None when git should decide.

    Two mistakes are worth catching before `git apply` sees them, because
    git's answer to both is "corrupt patch" and a line number. Both were
    observed on consecutive turns of one live run, the second being the
    model's attempt to correct the first.
    """
    if is_not_a_diff(diff):
        return (
            "this is not a unified diff. It needs, in this order: `--- <path>`, "
            "`+++ <path>`, a `@@ -<start>,<count> +<start>,<count> @@` hunk header, "
            "then the lines - '+' to add, '-' to remove, a leading space for a line "
            "that is already there and unchanged. Added lines on their own are not a "
            "patch, because nothing says where they go."
        )
    if changes_nothing(diff):
        return (
            "this diff changes nothing: every line in it is context. A line you are "
            "adding starts with '+' and a line you are removing starts with '-'; a "
            "leading space means 'this line is already there, unchanged'. Applying it "
            "would have reported success and left the file as it was."
        )
    return None


def _repair_notes(*, terminated: bool, created: Sequence[str], numbered: bool) -> str:
    """Return the notes that tell a model which clerical mistakes were repaired.

    A tool that silently fixes its caller teaches it nothing, so every repair
    is reported with the rule behind it.
    """
    notes: list[str] = []
    if terminated:
        notes.append(
            "note: the diff's last line had no newline, which makes git read the hunk "
            "as truncated. It was terminated for you; end a diff with a newline.",
        )
    if created:
        notes.append(
            f"note: {', '.join(created)} did not exist, so the `---` line was read as "
            "`--- /dev/null`. Announce a new file that way next time.",
        )
    if numbered:
        notes.append(
            "note: the hunk's lines carried line numbers. `read_file` adds those so you "
            "can read the file; they are not in it, so a diff that keeps them matches "
            "nothing. They were removed for you; write a diff from the file's own text, "
            "without the left margin.",
        )
    return "".join(f"{line}\n" for line in notes)


def _apply_refusal(diff: str, failures: Sequence[str]) -> str:
    """Return a refusal a model can act on.

    git reports a line number and calls the patch corrupt. The line itself is
    what explains it, so it is quoted, with the rule it breaks.
    """
    lines = diff.splitlines()
    # The last variant is the most lenient one, so its complaint is the one
    # that survived every repair - reporting the first would describe a
    # strictness the harness did not insist on.
    detail = failures[-1] if failures else "the diff does not apply"
    numbers = re.findall(r"patch at .*?:(\d+)", " ".join(failures))
    hint = ""
    if numbers:
        index = int(numbers[0]) - 1
        if 0 <= index < len(lines):
            offender = lines[index]
            if offender.startswith("@@"):
                hint = (
                    f"\nline {index + 1} is {offender[:80]!r}: a new hunk starts there, "
                    "but the previous hunk's `@@` counts had not been used up. Count "
                    "the lines in each hunk, or write one hunk per change."
                )
            elif offender[:1] not in DIFF_LINE_PREFIXES:
                hint = (
                    f"\nline {index + 1} is {offender[:80]!r}: inside a hunk every line "
                    "must start with a space, a '+' or a '-'. Text with no prefix ends "
                    "the hunk and makes the rest unreadable."
                )
    return (
        f"the diff does not apply:\n{detail}{hint}\n"
        "Write one hunk per change, with the file's real path on the --- and +++ "
        "lines, three lines of context above and below, and nothing after the last "
        "hunk line."
    )

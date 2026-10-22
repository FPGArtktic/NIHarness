# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The quality gate.

Nothing red is ever committed. `git_commit` is not a convenience wrapper, it is
a gate: format, lint, incremental build and quick tests must pass before the
commit happens. The full tier adds the complete build, integration tests,
sanitizers, bitbake and the documentation link check.

This module is pure. It decides *which* commands run, in which order, and what
a failure is called. Running them is the sandbox adapter's job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.identity import Digest, digest_text


class Language(StrEnum):
    """A toolchain the gate knows how to drive."""

    C_CPP = "c_cpp"
    PYTHON = "python"
    GO = "go"
    RUST = "rust"
    BASH = "bash"
    TCL = "tcl"
    HDL = "hdl"
    YOCTO = "yocto"
    DOCS = "docs"


class GateKind(StrEnum):
    """What a gate command does. Also the execution order."""

    FORMAT = "format"
    LINT = "lint"
    BUILD = "build"
    TEST = "test"
    DOCS = "docs"


KIND_ORDER: tuple[GateKind, ...] = (
    GateKind.FORMAT,
    GateKind.LINT,
    GateKind.BUILD,
    GateKind.TEST,
    GateKind.DOCS,
)
"""Cheap and specific first: a formatting failure must not wait for a test run."""


class GateTier(StrEnum):
    """The two tiers. Quick runs on every commit attempt, full before a merge."""

    QUICK = "quick"
    FULL = "full"


class GateCommand(BaseModel):
    """One command the gate runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Stable identifier, used in events and signatures.")
    argv: tuple[str, ...] = Field(min_length=1)
    kind: GateKind
    language: Language
    tiers: frozenset[GateTier] = Field(default=frozenset({GateTier.QUICK, GateTier.FULL}))
    apply_argv: tuple[str, ...] = ()
    """The same tool in its writing form, for a `format` command that has one.

    The gate asks a formatter whether the tree is formatted. A model told "it
    is not" has no way to act: it has `apply_patch` and must guess the
    formatter's exact output by hand. Measured on a live run, a correct, typed,
    documented function was refused for one missing blank line, and the model
    spent the rest of its budget failing to guess it.

    The policy prompt has always said style is the linter's job. This is what
    makes that true: `format_code` runs these, and the gate goes back to
    checking instead of blocking.
    """

    requires: tuple[str, ...] = ()
    """Files without which this command is not part of this project's gate.

    A marker file says a language is present (`pyproject.toml` means Python).
    This says a *check* is present: `lint-imports` is a real gate here because
    this repository configures import contracts, and it is nothing but a red
    command in a project that does not. Empty means the command belongs to the
    language itself - a Python project without `pytest` configured still has to
    answer for `pytest`.
    """


class LanguageGate(BaseModel):
    """The commands of one language, and the marker files that select it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    language: Language
    markers: tuple[str, ...]
    commands: tuple[GateCommand, ...]


def _command(
    name: str,
    argv: tuple[str, ...],
    kind: GateKind,
    language: Language,
    tiers: frozenset[GateTier] | None = None,
    requires: tuple[str, ...] = (),
    apply_argv: tuple[str, ...] = (),
) -> GateCommand:
    """Build a gate command, defaulting to both tiers and to no requirement."""
    return GateCommand(
        name=name,
        argv=argv,
        kind=kind,
        language=language,
        tiers=tiers if tiers is not None else frozenset({GateTier.QUICK, GateTier.FULL}),
        requires=requires,
        apply_argv=apply_argv,
    )


_FULL_ONLY = frozenset({GateTier.FULL})

GATE_MATRIX: dict[Language, LanguageGate] = {
    Language.C_CPP: LanguageGate(
        language=Language.C_CPP,
        markers=("CMakeLists.txt", "Makefile", "meson.build"),
        commands=(
            _command(
                "clang-format",
                ("clang-format", "--dry-run", "--Werror", "-style=file"),
                GateKind.FORMAT,
                Language.C_CPP,
                apply_argv=("clang-format", "-i", "-style=file"),
            ),
            _command(
                "clang-tidy",
                ("clang-tidy", "-p", "build"),
                GateKind.LINT,
                Language.C_CPP,
            ),
            _command(
                "cppcheck",
                ("cppcheck", "--error-exitcode=1", "."),
                GateKind.LINT,
                Language.C_CPP,
            ),
            _command(
                "cmake-build", ("cmake", "--build", "build"), GateKind.BUILD, Language.C_CPP
            ),
            _command(
                "ctest",
                ("ctest", "--test-dir", "build", "--output-on-failure"),
                GateKind.TEST,
                Language.C_CPP,
            ),
            _command(
                "ctest-asan",
                ("ctest", "--test-dir", "build-asan", "--output-on-failure"),
                GateKind.TEST,
                Language.C_CPP,
                _FULL_ONLY,
            ),
        ),
    ),
    Language.PYTHON: LanguageGate(
        language=Language.PYTHON,
        markers=("pyproject.toml", "setup.cfg"),
        commands=(
            _command(
                "ruff-format",
                ("ruff", "format", "--check", "."),
                GateKind.FORMAT,
                Language.PYTHON,
                apply_argv=("ruff", "format", "."),
            ),
            _command("ruff", ("ruff", "check", "."), GateKind.LINT, Language.PYTHON),
            _command("mypy", ("mypy", "--strict", "src"), GateKind.LINT, Language.PYTHON),
            _command(
                "lint-imports",
                ("lint-imports",),
                GateKind.LINT,
                Language.PYTHON,
                requires=(".importlinter", "setup.cfg", "pyproject.toml:tool.importlinter"),
            ),
            _command(
                "reuse",
                ("reuse", "lint"),
                GateKind.LINT,
                Language.PYTHON,
                requires=("REUSE.toml", ".reuse/dep5", "LICENSES"),
            ),
            _command("pytest", ("pytest",), GateKind.TEST, Language.PYTHON),
            _command(
                "pytest-integration",
                ("pytest", "-m", "integration"),
                GateKind.TEST,
                Language.PYTHON,
                _FULL_ONLY,
            ),
        ),
    ),
    Language.GO: LanguageGate(
        language=Language.GO,
        markers=("go.mod",),
        commands=(
            _command(
                "gofmt",
                ("gofmt", "-l", "."),
                GateKind.FORMAT,
                Language.GO,
                apply_argv=("gofmt", "-w", "."),
            ),
            _command("go-vet", ("go", "vet", "./..."), GateKind.LINT, Language.GO),
            _command("staticcheck", ("staticcheck", "./..."), GateKind.LINT, Language.GO),
            _command("go-build", ("go", "build", "./..."), GateKind.BUILD, Language.GO),
            _command("go-test", ("go", "test", "./..."), GateKind.TEST, Language.GO),
        ),
    ),
    Language.RUST: LanguageGate(
        language=Language.RUST,
        markers=("Cargo.toml",),
        commands=(
            _command(
                "cargo-fmt",
                ("cargo", "fmt", "--check"),
                GateKind.FORMAT,
                Language.RUST,
                apply_argv=("cargo", "fmt"),
            ),
            _command(
                "cargo-clippy",
                ("cargo", "clippy", "--", "-D", "warnings"),
                GateKind.LINT,
                Language.RUST,
            ),
            _command("cargo-build", ("cargo", "build"), GateKind.BUILD, Language.RUST),
            _command("cargo-test", ("cargo", "test"), GateKind.TEST, Language.RUST),
            _command(
                "cargo-deny",
                ("cargo", "deny", "check", "licenses"),
                GateKind.LINT,
                Language.RUST,
                _FULL_ONLY,
            ),
        ),
    ),
    Language.BASH: LanguageGate(
        language=Language.BASH,
        markers=(".shellcheckrc",),
        commands=(
            _command(
                "shfmt",
                ("shfmt", "-d", "."),
                GateKind.FORMAT,
                Language.BASH,
                apply_argv=("shfmt", "-w", "."),
            ),
            _command("shellcheck", ("shellcheck", "-S", "style"), GateKind.LINT, Language.BASH),
            _command("bats", ("bats", "tests"), GateKind.TEST, Language.BASH),
        ),
    ),
    Language.TCL: LanguageGate(
        language=Language.TCL,
        markers=("tclIndex", "pkgIndex.tcl"),
        commands=(
            _command("tclint", ("tclint", "."), GateKind.LINT, Language.TCL),
            _command("tcltest", ("tclsh", "tests/all.tcl"), GateKind.TEST, Language.TCL),
        ),
    ),
    Language.HDL: LanguageGate(
        language=Language.HDL,
        markers=(".verible-format", "vunit_run.py"),
        commands=(
            _command(
                "verible-format",
                ("verible-verilog-format", "--verify"),
                GateKind.FORMAT,
                Language.HDL,
                apply_argv=("verible-verilog-format", "--inplace"),
            ),
            _command(
                "verilator-lint", ("verilator", "--lint-only"), GateKind.LINT, Language.HDL
            ),
            _command("vsg", ("vsg", "-c", "vsg.yaml"), GateKind.LINT, Language.HDL),
            _command("ghdl-analyze", ("ghdl", "-a"), GateKind.BUILD, Language.HDL),
            _command("cocotb", ("make", "-C", "tb"), GateKind.TEST, Language.HDL, _FULL_ONLY),
        ),
    ),
    Language.YOCTO: LanguageGate(
        language=Language.YOCTO,
        markers=("conf/layer.conf",),
        commands=(
            _command(
                "oelint-adv",
                ("oelint-adv", "recipes-*/*/*.bb"),
                GateKind.LINT,
                Language.YOCTO,
                _FULL_ONLY,
            ),
            _command(
                "bitbake-parse", ("bitbake", "-p"), GateKind.LINT, Language.YOCTO, _FULL_ONLY
            ),
            _command(
                "bitbake",
                ("bitbake", "core-image-minimal"),
                GateKind.BUILD,
                Language.YOCTO,
                _FULL_ONLY,
            ),
            _command(
                "ptest",
                ("bitbake", "-c", "testimage", "core-image-minimal"),
                GateKind.TEST,
                Language.YOCTO,
                _FULL_ONLY,
            ),
        ),
    ),
    Language.DOCS: LanguageGate(
        language=Language.DOCS,
        markers=("mkdocs.yml",),
        commands=(
            _command("markdownlint", ("markdownlint", "docs"), GateKind.LINT, Language.DOCS),
            _command(
                "mkdocs-strict", ("mkdocs", "build", "--strict"), GateKind.DOCS, Language.DOCS
            ),
            _command(
                "mkdocs-linkcheck",
                ("mkdocs", "build", "--strict", "--config-file", "mkdocs.linkcheck.yml"),
                GateKind.DOCS,
                Language.DOCS,
                _FULL_ONLY,
            ),
        ),
    ),
}
"""Default per-language matrix; `harness.toml` overrides it under `[gate.<lang>]`."""

DOC_PREFIXES: tuple[str, ...] = ("docs/", "mkdocs.yml", "README.md", "CHANGELOG.md")
"""Changed paths that make the documentation build part of the quick tier."""


def detect_languages(repo_files: Iterable[str]) -> frozenset[Language]:
    """Return the languages whose marker files are present.

    Args:
        repo_files: worktree-relative paths, as the sandbox lists them.

    Returns:
        The detected languages. Detection is by marker file only, never by
        extension count, so adding a stray `.go` file does not pull in the Go
        toolchain.
    """
    present = set(repo_files)
    basenames = {path.rsplit("/", 1)[-1] for path in present}
    detected: set[Language] = set()
    for gate in GATE_MATRIX.values():
        for marker in gate.markers:
            if marker in present or marker in basenames:
                detected.add(gate.language)
                break
    return frozenset(detected)


def plan_gate(
    tier: GateTier,
    languages: Iterable[Language],
    changed_paths: Iterable[str] = (),
    repo_files: Iterable[str] = (),
) -> tuple[GateCommand, ...]:
    """Return the commands to run, in execution order.

    Args:
        tier: quick on every commit attempt, full before a merge.
        languages: the languages detected in the worktree.
        changed_paths: paths touched since the last green commit. In the quick
            tier the documentation build runs only when documentation changed.
        repo_files: the worktree's files. A command that declares `requires`
            runs only if one of those files is there; without a listing,
            nothing is filtered, because an empty listing is not evidence of an
            empty worktree.

    Returns:
        The ordered command list. Yocto never appears in the quick tier.
    """
    selected = set(languages)
    changed = tuple(changed_paths)
    if tier is GateTier.QUICK and Language.DOCS in selected:
        touched_docs = any(
            path == prefix or path.startswith(prefix)
            for path in changed
            for prefix in DOC_PREFIXES
        )
        if changed and not touched_docs:
            selected.discard(Language.DOCS)

    present = frozenset(repo_files)
    commands = [
        command
        for language in sorted(selected, key=lambda item: item.value)
        for command in GATE_MATRIX[language].commands
        if tier in command.tiers and _configured(command, present)
    ]
    commands.sort(
        key=lambda command: (
            KIND_ORDER.index(command.kind),
            command.language.value,
            command.name,
        )
    )
    return tuple(commands)


TOOL_TABLE = re.compile(r"^\[\[?tool\.(?P<name>[A-Za-z0-9_-]+)")
"""A `[tool.x]` or `[[tool.x.y]]` table header in a `pyproject.toml`."""


def gate_listing(files: Iterable[str], pyproject: str = "") -> frozenset[str]:
    """Return the listing :func:`plan_gate` plans against.

    The file names, plus one entry per tool table a `pyproject.toml` declares,
    spelled `pyproject.toml:tool.<name>`. That is what tells a project which
    configures `[tool.importlinter]` from one that has merely heard of
    `pyproject.toml`, without the domain reading a file: the caller does the
    reading and this does the parsing, which is pure and testable.

    Args:
        files: worktree-relative paths, as the sandbox lists them.
        pyproject: the text of `pyproject.toml`, when there is one.

    Returns:
        The listing, with the declared tables added.
    """
    listing = set(files)
    for line in pyproject.splitlines():
        match = TOOL_TABLE.match(line.strip())
        if match is not None:
            listing.add(f"pyproject.toml:tool.{match['name']}")
    return frozenset(listing)


def format_commands(
    languages: Iterable[Language],
    repo_files: Iterable[str] = (),
) -> tuple[GateCommand, ...]:
    """Return the formatters that can write, for the detected languages.

    The same selection the gate makes, narrowed to `format` commands that have
    a writing form. A language whose formatter only complains does not appear,
    and neither does one this project has not configured.
    """
    present = frozenset(repo_files)
    commands = [
        command
        for language in sorted(set(languages), key=lambda item: item.value)
        for command in GATE_MATRIX[language].commands
        if command.kind is GateKind.FORMAT
        and command.apply_argv
        and _configured(command, present)
    ]
    return tuple(sorted(commands, key=lambda command: (command.language.value, command.name)))


def _configured(command: GateCommand, present: frozenset[str]) -> bool:
    """Return whether this project has what `command` checks.

    A requirement is met by an exact path, by a directory the listing has files
    under (`LICENSES` is met by `LICENSES/GPL-3.0-or-later.txt`), or by a
    `pyproject.toml:tool.<name>` entry from :func:`gate_listing`.
    """
    if not command.requires or not present:
        return True
    return any(
        requirement in present or any(path.startswith(f"{requirement}/") for path in present)
        for requirement in command.requires
    )


class GateStepResult(BaseModel):
    """The outcome of one gate command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    exit_code: int
    duration_seconds: float = Field(ge=0)
    output: str = ""

    @property
    def passed(self) -> bool:
        """True when the command exited zero."""
        return self.exit_code == 0


class GateResult(BaseModel):
    """The outcome of a gate run. Red means no commit, for agents and humans alike."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: GateTier
    steps: tuple[GateStepResult, ...]

    @property
    def passed(self) -> bool:
        """True when every command passed. An empty gate does not pass."""
        return bool(self.steps) and all(step.passed for step in self.steps)

    @property
    def failures(self) -> tuple[GateStepResult, ...]:
        """The failing steps, in execution order."""
        return tuple(step for step in self.steps if not step.passed)

    def failure_signature(self) -> str:
        """Return a stable signature of this failure.

        Two runs that fail the same way share a signature, which is what the
        gate-thrash detector counts. Paths, line numbers, durations and
        addresses are normalised away.
        """
        if self.passed:
            return ""
        parts = [f"{step.command}:{normalize_output(step.output)}" for step in self.failures]
        return str(digest_text("\n".join(parts)))


_RE_HEX = re.compile(r"0x[0-9a-fA-F]+")
_RE_PATH = re.compile(r"(?:/[\w.+-]+){2,}")
_RE_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|m)\b")
_RE_NUMBER = re.compile(r"\b\d+\b")
_RE_WS = re.compile(r"\s+")
_INTERESTING = (
    "error",
    "failed",
    "failure",
    "warning",
    "panicked",
    "assert",
    "e: ",
    "fatal",
)
MAX_SIGNATURE_LINES = 12
"""How many interesting lines contribute to a failure signature."""


def normalize_output(output: str, *, max_lines: int = MAX_SIGNATURE_LINES) -> str:
    """Normalise command output into a comparable failure shape.

    Args:
        output: raw stdout and stderr of a gate command.
        max_lines: how many interesting lines to keep.

    Returns:
        A normalised string: only the lines that look like diagnostics, with
        paths, numbers, durations and addresses replaced by placeholders.
    """
    kept: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if not any(token in lowered for token in _INTERESTING):
            continue
        line = _RE_HEX.sub("<addr>", line)
        line = _RE_PATH.sub("<path>", line)
        line = _RE_DURATION.sub("<dur>", line)
        line = _RE_NUMBER.sub("<n>", line)
        line = _RE_WS.sub(" ", line).strip()
        if line not in kept:
            kept.append(line)
        if len(kept) == max_lines:
            break
    return "\n".join(kept)


def output_digest(output: str) -> Digest:
    """Return the digest of normalised output, for event payloads."""
    return digest_text(normalize_output(output))

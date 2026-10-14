# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool contracts and call validation.

Models never get a host shell: every call in this module is executed by an MCP
server inside the sandbox container. Before anything is executed the call is
validated here - schema, allowlist, path confinement, argument rules - and the
validation is pure, so it is unit-testable and identical on replay.

Lexical path confinement happens here. Resolving symlinks is the sandbox
adapter's job, because only it can touch a filesystem.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from niharness.domain.identity import CallId, Digest, digest_json

RelPath = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)
]
"""A path, validated for confinement by :func:`confine_path`."""


class ToolName(StrEnum):
    """Every tool the harness can expose. Allowlists are per role, never global."""

    READ_FILE = "read_file"
    LIST_TREE = "list_tree"
    GREP = "grep"
    RAG_QUERY = "rag_query"
    APPLY_PATCH = "apply_patch"
    RUN_LINT = "run_lint"
    RUN_BUILD = "run_build"
    RUN_TESTS = "run_tests"
    GIT_COMMIT = "git_commit"
    ADD_DEPENDENCY = "add_dependency"
    SHELL = "shell"


READ_ONLY_TOOLS: frozenset[ToolName] = frozenset(
    {ToolName.READ_FILE, ToolName.LIST_TREE, ToolName.GREP, ToolName.RAG_QUERY},
)
"""Tools that cannot change the worktree; the researcher and brainstormer get only these."""


class Violation(StrEnum):
    """Why a call was rejected. Every rejection is a `tool.rejected` event."""

    UNKNOWN_TOOL = "unknown_tool"
    NOT_ALLOWED = "not_allowed"
    INVALID_ARGUMENTS = "invalid_arguments"
    ABSOLUTE_PATH = "absolute_path"
    PATH_ESCAPE = "path_escape"
    DIFF_TOO_LARGE = "diff_too_large"
    COMMIT_MESSAGE = "commit_message"


class ToolLimits(BaseModel):
    """Caps the harness enforces on tool arguments and results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_diff_bytes: int = Field(default=64_000, ge=1)
    max_result_bytes: int = Field(default=16_000, ge=256)
    max_read_lines: int = Field(default=2_000, ge=1)
    max_grep_matches: int = Field(default=200, ge=1)
    max_commit_subject: int = Field(default=72, ge=20)
    max_shell_seconds: int = Field(default=120, ge=1)


# ----------------------------------------------------------------- argument sets


class ToolArgs(BaseModel):
    """Base class for tool argument models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ReadFileArgs(ToolArgs):
    """Read a slice of one file inside the worktree."""

    path: RelPath
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ListTreeArgs(ToolArgs):
    """List a directory, bounded in depth."""

    path: RelPath = "."
    depth: int = Field(default=2, ge=1, le=8)


class GrepArgs(ToolArgs):
    """Search the worktree for a regular expression."""

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    path: RelPath = "."
    glob: str = ""
    max_matches: int = Field(default=50, ge=1)


class RagQueryArgs(ToolArgs):
    """Query the vector store, including context compacted out of the window."""

    query: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    top_k: int = Field(default=8, ge=1, le=50)
    collection: str = ""


class ApplyPatchArgs(ToolArgs):
    """Apply a unified diff. There is no write_file: every change is a reviewable diff."""

    diff: Annotated[str, StringConstraints(min_length=1)]


class RunLintArgs(ToolArgs):
    """Run the configured linters, optionally narrowed to a path."""

    path: RelPath = "."


class RunBuildArgs(ToolArgs):
    """Run the configured build. The reviewer runs this itself."""

    target: str = ""


class RunTestsArgs(ToolArgs):
    """Run tests, optionally narrowed by a selector."""

    selector: str = ""


class GitCommitArgs(ToolArgs):
    """Attempt a commit. This is the gate: format, lint, build, quick tests."""

    message: Annotated[str, StringConstraints(min_length=10, max_length=4096)]


class AddDependencyArgs(ToolArgs):
    """Request a dependency. Rejected unless it is already in the local mirror."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    version: str = ""
    ecosystem: Literal["python", "rust", "system"] = "python"


class ShellArgs(ToolArgs):
    """Run a command in the sandbox. Not in any default allowlist."""

    command: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    timeout_seconds: int = Field(default=60, ge=1)


TOOL_ARGUMENTS: dict[ToolName, type[ToolArgs]] = {
    ToolName.READ_FILE: ReadFileArgs,
    ToolName.LIST_TREE: ListTreeArgs,
    ToolName.GREP: GrepArgs,
    ToolName.RAG_QUERY: RagQueryArgs,
    ToolName.APPLY_PATCH: ApplyPatchArgs,
    ToolName.RUN_LINT: RunLintArgs,
    ToolName.RUN_BUILD: RunBuildArgs,
    ToolName.RUN_TESTS: RunTestsArgs,
    ToolName.GIT_COMMIT: GitCommitArgs,
    ToolName.ADD_DEPENDENCY: AddDependencyArgs,
    ToolName.SHELL: ShellArgs,
}
"""The argument model of every tool; also the source of the JSON Schemas sent to the model."""

PATH_FIELDS: frozenset[str] = frozenset({"path"})
"""Argument names subject to path confinement."""


# ----------------------------------------------------------------- call and result


class ToolCall(BaseModel):
    """A call as proposed by the model, before validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: CallId
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    def arguments_digest(self) -> Digest:
        """Return a canonical digest of the arguments, used by the repeat detector."""
        return digest_json(self.arguments)


class AcceptedCall(BaseModel):
    """A call that passed validation, with its arguments parsed into a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: Literal[True] = Field(
        default=True,
        description="Discriminator, so callers narrow the union without isinstance.",
    )
    call_id: CallId
    tool: ToolName
    args: ToolArgs


class RejectedCall(BaseModel):
    """A call that failed validation and must never reach the sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: Literal[False] = Field(
        default=False,
        description="Discriminator, so callers narrow the union without isinstance.",
    )
    call_id: CallId
    name: str
    violation: Violation
    detail: str


Validation = Annotated[AcceptedCall | RejectedCall, Field(discriminator="accepted")]
"""The result of :func:`validate_call`; branch on `accepted`."""


class ToolResult(BaseModel):
    """A structured, truncated tool result as it enters the window and the event log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: CallId
    tool: ToolName
    ok: bool
    content: str
    truncated: bool = False
    summary: str = Field(default="", description="One line for the window digest.")
    exit_code: int | None = None

    def result_digest(self) -> Digest:
        """Return a digest of the content, used by the progress-stall detector."""
        return digest_json({"ok": self.ok, "content": self.content})


# ---------------------------------------------------------------------- validation

COMMIT_SUBJECT = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\((?P<scope>[a-z0-9._/-]+)\))?(?P<breaking>!)?: (?P<subject>.+)$",
)
"""Conventional Commits subject line; the gate validates the message before committing."""


def confine_path(candidate: str) -> PurePosixPath:
    """Return `candidate` as a worktree-relative path.

    Args:
        candidate: the path as the model wrote it.

    Returns:
        The normalised relative path.

    Raises:
        PathConfinementError: if the path is absolute or escapes the worktree
            lexically. Symlink resolution is the sandbox adapter's duty.
    """
    path = PurePosixPath(candidate)
    if path.is_absolute() or candidate.startswith("~"):
        raise PathConfinementError(Violation.ABSOLUTE_PATH, candidate)
    parts: list[str] = []
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise PathConfinementError(Violation.PATH_ESCAPE, candidate)
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else PurePosixPath(".")


class PathConfinementError(ValueError):
    """A path argument left the worktree."""

    def __init__(self, violation: Violation, candidate: str) -> None:
        self.violation = violation
        self.candidate = candidate
        super().__init__(f"{violation.value}: {candidate!r}")


def validate_commit_message(message: str, limits: ToolLimits) -> str:
    """Return the commit subject.

    Args:
        message: the full commit message.
        limits: caps in force for this step.

    Returns:
        The validated subject line.

    Raises:
        ValueError: if the subject is not a Conventional Commits subject, is
            too long, or the message has no body explaining why.
    """
    lines = message.strip().splitlines()
    subject = lines[0].strip() if lines else ""
    if not COMMIT_SUBJECT.match(subject):
        msg = f"subject is not Conventional Commits: {subject!r}"
        raise ValueError(msg)
    if len(subject) > limits.max_commit_subject:
        msg = f"subject is {len(subject)} characters, limit is {limits.max_commit_subject}"
        raise ValueError(msg)
    body = "\n".join(lines[1:]).strip()
    if not body:
        msg = "commit message has no body; the body explains why the change is made"
        raise ValueError(msg)
    return subject


def validate_call(
    call: ToolCall,
    allowlist: frozenset[ToolName],
    limits: ToolLimits,
) -> Validation:
    """Validate one proposed tool call.

    The checks run in a fixed order so that a rejection reason is stable:
    the tool exists, the role may use it, the arguments match the schema,
    every path stays inside the worktree, then per-tool argument rules.

    Args:
        call: the call as proposed by the model.
        allowlist: the tools this step's role may use.
        limits: caps in force for this step.

    Returns:
        An :class:`AcceptedCall` with parsed arguments, or a :class:`RejectedCall`.
    """
    try:
        tool = ToolName(call.name)
    except ValueError:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.UNKNOWN_TOOL,
            detail=f"no such tool; available: {', '.join(sorted(t.value for t in allowlist))}",
        )

    if tool not in allowlist:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.NOT_ALLOWED,
            detail=f"{tool.value} is not in this role's allowlist",
        )

    try:
        args = TOOL_ARGUMENTS[tool].model_validate(call.arguments)
    except ValueError as error:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.INVALID_ARGUMENTS,
            detail=str(error),
        )

    for field in PATH_FIELDS & set(type(args).model_fields):
        raw = getattr(args, field)
        if not isinstance(raw, str):  # pragma: no cover - models declare paths as str
            continue
        try:
            confine_path(raw)
        except PathConfinementError as error:
            return RejectedCall(
                call_id=call.call_id,
                name=call.name,
                violation=error.violation,
                detail=f"{field}={raw!r} is outside the worktree",
            )

    if isinstance(args, ApplyPatchArgs):
        size = len(args.diff.encode("utf-8"))
        if size > limits.max_diff_bytes:
            return RejectedCall(
                call_id=call.call_id,
                name=call.name,
                violation=Violation.DIFF_TOO_LARGE,
                detail=f"diff is {size} bytes, limit is {limits.max_diff_bytes}",
            )

    if isinstance(args, GitCommitArgs):
        try:
            validate_commit_message(args.message, limits)
        except ValueError as error:
            return RejectedCall(
                call_id=call.call_id,
                name=call.name,
                violation=Violation.COMMIT_MESSAGE,
                detail=str(error),
            )

    return AcceptedCall(call_id=call.call_id, tool=tool, args=args)


def truncate_result(content: str, limits: ToolLimits) -> tuple[str, bool]:
    """Truncate a tool result to the configured size.

    Args:
        content: the raw result text.
        limits: caps in force for this step.

    Returns:
        The possibly truncated text and whether truncation happened. The head
        and the tail are kept, because errors live at the end of build output.
    """
    encoded = content.encode("utf-8")
    if len(encoded) <= limits.max_result_bytes:
        return content, False
    keep = limits.max_result_bytes // 2
    head = encoded[:keep].decode("utf-8", errors="ignore")
    tail = encoded[-keep:].decode("utf-8", errors="ignore")
    elided = len(encoded) - 2 * keep
    return f"{head}\n... [{elided} bytes elided] ...\n{tail}", True

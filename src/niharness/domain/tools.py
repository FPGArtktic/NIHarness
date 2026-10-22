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

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from niharness.domain.identity import CallId, Digest, digest_json

RelPath = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)
]
"""A path, validated for confinement by :func:`confine_path`."""

NAMESPACE = "__"
"""Separates a configured server's name from the name of the tool it advertises.

No built-in tool contains it, so an external server cannot have the harness
mistake one of its tools for `git_commit`: whatever a server calls its tool,
the harness, the model and the event log address it as `<server>__<tool>`.
"""

TOOL_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:__[A-Za-z0-9_.-]{1,64})?$"
"""A built-in tool name, or an external tool's namespaced address."""

ToolId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=TOOL_ID_PATTERN,
    ),
]
"""How a tool is addressed: a :class:`ToolName` value, or `<server>__<tool>`."""


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
    ARGUMENTS_TOO_LARGE = "arguments_too_large"
    COMMIT_MESSAGE = "commit_message"


class ToolLimits(BaseModel):
    """Caps the harness enforces on tool arguments and results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_diff_bytes: int = Field(default=64_000, ge=1)
    max_result_bytes: int = Field(default=16_000, ge=256)
    max_argument_bytes: int = Field(
        default=16_000,
        ge=256,
        description="Cap on an external tool's arguments, which the harness cannot model.",
    )
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


class ExternalArgs(ToolArgs):
    """Arguments for a tool the harness does not model itself.

    Extras are kept rather than forbidden, because the shape belongs to the
    server that advertised it. They are checked against that advertised schema
    by :func:`check_arguments` and capped by `max_argument_bytes`; nothing else
    is assumed about them.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


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


# ------------------------------------------------------- tools from elsewhere


class ExternalTool(BaseModel):
    """A tool the harness forwards from a configured MCP server.

    The harness has no argument model for it: `parameters` is the schema the
    server advertised, carried as data so the model can be told what the tool
    takes and so a plainly malformed call can be refused before it is sent. It
    is a description, not a promise about what the server will do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    roles: frozenset[str] = frozenset()
    """Roles granted this tool, as names - strings, so this module stays below `roles`."""

    @property
    def id(self) -> str:
        """Return the namespaced address the model, the log and the router use."""
        return f"{self.server}{NAMESPACE}{self.name}"


class ToolCatalog(BaseModel):
    """The external tools a run may reach, and which roles may reach them.

    A run with no `[[mcp.servers]]` in `harness.toml` carries
    :data:`EMPTY_CATALOG`, and then nothing in the harness behaves differently
    from before this existed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    external: tuple[ExternalTool, ...] = ()

    def get(self, tool_id: str) -> ExternalTool | None:
        """Return the external tool addressed by `tool_id`, or None when there is none."""
        for tool in self.external:
            if tool.id == tool_id:
                return tool
        return None

    def allowlist_for(self, role: str) -> frozenset[ToolId]:
        """Return the ids of the external tools `role` was granted."""
        return frozenset(tool.id for tool in self.external if role in tool.roles)

    def servers(self) -> tuple[str, ...]:
        """Return the server names, in the order they were configured, without repeats."""
        return tuple(dict.fromkeys(tool.server for tool in self.external))

    def tools_of(self, server: str) -> tuple[str, ...]:
        """Return the tool names imported from `server`."""
        return tuple(tool.name for tool in self.external if tool.server == server)

    def roles_of(self, server: str) -> tuple[str, ...]:
        """Return the roles any of `server`'s tools were granted to, sorted."""
        granted: set[str] = set()
        for tool in self.external:
            if tool.server == server:
                granted |= tool.roles
        return tuple(sorted(granted))


EMPTY_CATALOG = ToolCatalog()
"""No external servers, which is what a project gets until it configures one."""


def is_external(name: str) -> bool:
    """Return whether `name` addresses a tool outside the harness's own set."""
    return NAMESPACE in name


# --------------------------------------------- the schema subset we can check

JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}
"""The JSON Schema types :func:`check_arguments` understands."""


class SchemaMismatchError(ValueError):
    """An argument contradicted the schema the server advertised for its tool."""


def check_arguments(
    schema: Mapping[str, JsonValue],
    value: JsonValue,
    path: str = "arguments",
) -> None:
    """Check `value` against the part of JSON Schema the harness honours.

    Honoured: `type`, `enum`, `required`, `properties`, `additionalProperties`
    when it is `false`, and `items`. Formats, numeric bounds and the combinators
    are left to the server, which is the only side that knows what its tool
    means. This is not a validator and does not pretend to be one: its job is
    to turn an obviously malformed call into a rejection the model can read,
    instead of a transport error it cannot.

    Args:
        schema: the advertised schema, or a sub-schema during recursion.
        value: the arguments, or a nested value during recursion.
        path: where in the arguments this value sits, for the message.

    Raises:
        SchemaMismatchError: when the value contradicts the schema.
    """
    _check_type(schema, value, path)
    _check_enum(schema, value, path)
    if isinstance(value, dict):
        _check_object(schema, value, path)
        return
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, element in enumerate(value):
                check_arguments(items, element, f"{path}[{index}]")


def _declared_types(schema: Mapping[str, JsonValue]) -> list[str]:
    """Return the schema's `type` as a list of names the harness knows."""
    declared = schema.get("type")
    if isinstance(declared, str):
        names: list[JsonValue] = [declared]
    elif isinstance(declared, list):
        names = declared
    else:
        return []
    return [name for name in names if isinstance(name, str) and name in JSON_TYPES]


def _is_type(name: str, value: JsonValue) -> bool:
    """Return whether `value` is of the JSON type `name`."""
    if isinstance(value, bool) and name in {"integer", "number"}:
        # True is an int in Python; in JSON it is not a number.
        return False
    return isinstance(value, JSON_TYPES[name])


def _type_of(value: JsonValue) -> str:
    """Return the JSON type name of `value`, for a message a model can act on."""
    for name in ("boolean", "null", "string", "integer", "number", "array", "object"):
        if _is_type(name, value):
            return name
    return type(value).__name__  # pragma: no cover - JSON has no other types


def _check_type(schema: Mapping[str, JsonValue], value: JsonValue, path: str) -> None:
    """Raise unless `value` has one of the types the schema declares.

    Raises:
        SchemaMismatchError: when it has none of them.
    """
    declared = _declared_types(schema)
    if not declared or any(_is_type(name, value) for name in declared):
        return
    wanted = " or ".join(declared)
    raise SchemaMismatchError(f"{path} is {_type_of(value)}, the schema says {wanted}")


def _check_enum(schema: Mapping[str, JsonValue], value: JsonValue, path: str) -> None:
    """Raise unless `value` is one of the schema's permitted values.

    Raises:
        SchemaMismatchError: when the enumeration does not contain it.
    """
    permitted = schema.get("enum")
    if not isinstance(permitted, list) or value in permitted:
        return
    options = ", ".join(repr(option) for option in permitted)
    raise SchemaMismatchError(f"{path}={value!r} is not one of {options}")


def _check_object(
    schema: Mapping[str, JsonValue],
    value: Mapping[str, JsonValue],
    path: str,
) -> None:
    """Raise unless `value` has the required keys, no refused ones, and valid members.

    Raises:
        SchemaMismatchError: on a missing key, an unexpected key, or a member
            that contradicts its own sub-schema.
    """
    declared = schema.get("properties")
    properties: Mapping[str, JsonValue] = declared if isinstance(declared, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, list):
        missing = sorted(key for key in required if isinstance(key, str) and key not in value)
        if missing:
            raise SchemaMismatchError(f"{path} is missing {', '.join(missing)}")
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(value) - set(properties))
        if unexpected:
            raise SchemaMismatchError(f"{path} does not take {', '.join(unexpected)}")
    for key, member in properties.items():
        if key in value and isinstance(member, Mapping):
            check_arguments(member, value[key], f"{path}.{key}")


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
    tool: ToolId
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
    tool: ToolId
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
    allowlist: frozenset[ToolId],
    limits: ToolLimits,
    catalog: ToolCatalog = EMPTY_CATALOG,
) -> Validation:
    """Validate one proposed tool call.

    The checks run in a fixed order so that a rejection reason is stable:
    the tool exists, the role may use it, the arguments match the schema,
    every path stays inside the worktree, then per-tool argument rules.

    Args:
        call: the call as proposed by the model.
        allowlist: the tools this step's role may use.
        limits: caps in force for this step.
        catalog: the external tools this run reached, if any.

    Returns:
        An :class:`AcceptedCall` with parsed arguments, or a :class:`RejectedCall`.
    """
    if is_external(call.name):
        return _validate_external(call, allowlist, limits, catalog)
    try:
        tool = ToolName(call.name)
    except ValueError:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.UNKNOWN_TOOL,
            detail=f"no such tool; available: {_offered(allowlist)}",
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


def _offered(allowlist: frozenset[ToolId]) -> str:
    """Return the allowed tools as a list the model can read back."""
    return ", ".join(sorted(str(tool) for tool in allowlist))


def _validate_external(
    call: ToolCall,
    allowlist: frozenset[ToolId],
    limits: ToolLimits,
    catalog: ToolCatalog,
) -> Validation:
    """Validate a call to a tool a configured MCP server advertised.

    There is no path confinement here, and that is deliberate: the harness
    cannot confine arguments whose meaning it does not know. What bounds an
    external tool is everything upstream of this function - a human named the
    server in `harness.toml`, named the tools to import, and named the roles
    that may use them. Nothing else on that server is reachable, and the gate
    is still the only way a change becomes a commit.

    Args:
        call: the call as proposed by the model.
        allowlist: the tools this step's role may use.
        limits: caps in force for this step.
        catalog: the external tools this run reached.

    Returns:
        An :class:`AcceptedCall` carrying the arguments as advertised, or a
        :class:`RejectedCall`.
    """
    tool = catalog.get(call.name)
    if tool is None:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.UNKNOWN_TOOL,
            detail=f"no such tool; available: {_offered(allowlist)}",
        )
    if tool.id not in allowlist:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.NOT_ALLOWED,
            detail=f"{tool.id} is not in this role's allowlist",
        )
    size = len(json.dumps(call.arguments, sort_keys=True, default=str).encode("utf-8"))
    if size > limits.max_argument_bytes:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.ARGUMENTS_TOO_LARGE,
            detail=f"arguments are {size} bytes, limit is {limits.max_argument_bytes}",
        )
    try:
        check_arguments(tool.parameters, call.arguments)
    except SchemaMismatchError as error:
        return RejectedCall(
            call_id=call.call_id,
            name=call.name,
            violation=Violation.INVALID_ARGUMENTS,
            detail=str(error),
        )
    return AcceptedCall(
        call_id=call.call_id,
        tool=tool.id,
        args=ExternalArgs.model_validate(call.arguments),
    )


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

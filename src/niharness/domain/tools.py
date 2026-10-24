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
    FIND_SYMBOL = "find_symbol"
    FIND_REFERENCES = "find_references"
    FILE_OUTLINE = "file_outline"
    RAG_QUERY = "rag_query"
    REPLACE_IN_FILE = "replace_in_file"
    APPLY_PATCH = "apply_patch"
    FORMAT_CODE = "format_code"
    RUN_LINT = "run_lint"
    RUN_BUILD = "run_build"
    RUN_TESTS = "run_tests"
    GIT_COMMIT = "git_commit"
    ADD_DEPENDENCY = "add_dependency"
    SHELL = "shell"


HARNESS_SERVED_TOOLS: frozenset[ToolName] = frozenset({ToolName.RAG_QUERY})
"""Tools the harness serves in its own process, which the sandbox never holds.

The sandbox runs `--network=none` and these need a service on the host, so
routing one into the container reaches nothing (ADR 0021). Naming the set here
rather than in an adapter is what lets the router say so precisely: a tool in
a role's allowlist that nothing is serving has to be refused with the reason -
retrieval is not configured - and not with a transport error blaming the tool
server for a configuration fact.
"""


READ_ONLY_TOOLS: frozenset[ToolName] = frozenset(
    {
        ToolName.READ_FILE,
        ToolName.LIST_TREE,
        ToolName.GREP,
        ToolName.FIND_SYMBOL,
        ToolName.FIND_REFERENCES,
        ToolName.FILE_OUTLINE,
        ToolName.RAG_QUERY,
    },
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


class FindSymbolArgs(ToolArgs):
    """Ask a language server where a symbol is defined, by name.

    By name and not by position: a model that has to count columns before it
    can ask a question has already read the file, which is the cost the tool
    exists to avoid.
    """

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    language: str = Field(
        default="",
        description="Narrow the search to one language, when more than one could answer.",
    )


class FindReferencesArgs(ToolArgs):
    """Ask a language server who uses a symbol, by name."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    max_results: int = Field(default=30, ge=1, le=200)
    language: str = ""


class FileOutlineArgs(ToolArgs):
    """Ask a language server what is in a file, without reading the file."""

    path: RelPath


class RagQueryArgs(ToolArgs):
    """Query the vector store, including context compacted out of the window."""

    query: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    top_k: int = Field(default=8, ge=1, le=50)
    collection: str = ""


class ReplaceInFileArgs(ToolArgs):
    """Replace one exact passage of a file with another.

    The passage is located by its own text, not by a line number, for the same
    reason the symbol tools take a name: a model that has to count lines has
    already read the file, and will still count wrong. `find` has to occur
    exactly once, which is what makes the edit unambiguous without a position.

    An empty `replace` deletes the passage. There is no option to replace every
    occurrence: a change that means five places is five edits, each of which
    can be refused on its own.

    An empty `find` **creates** the file, with `replace` as its whole content.
    The schema cannot say "empty only when the path does not exist", because
    whether it exists is I/O; the tool server refuses an empty `find` against
    a file that is there, since it would not say what to change (ADR 0025).
    """

    path: RelPath
    find: str = Field(
        description="The exact text to replace, copied from the file. "
        "Empty creates the file instead, with `replace` as its content.",
    )
    replace: str = Field(description="What to put in its place; empty deletes it.")


class ApplyPatchArgs(ToolArgs):
    """Apply a unified diff. There is no write_file: every change is a reviewable diff."""

    diff: Annotated[str, StringConstraints(min_length=1)]


class FormatCodeArgs(ToolArgs):
    """Run the project's formatters, so style is not something to guess at."""

    path: RelPath = "."


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
    ToolName.FIND_SYMBOL: FindSymbolArgs,
    ToolName.FIND_REFERENCES: FindReferencesArgs,
    ToolName.FILE_OUTLINE: FileOutlineArgs,
    ToolName.RAG_QUERY: RagQueryArgs,
    ToolName.REPLACE_IN_FILE: ReplaceInFileArgs,
    ToolName.APPLY_PATCH: ApplyPatchArgs,
    ToolName.FORMAT_CODE: FormatCodeArgs,
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


COMMIT_TYPES: tuple[str, ...] = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)
"""The types `COMMIT_SUBJECT` accepts, in one place so a message can list them."""

_FIX_WORDS = ("fix", "correct", "repair", "restore", "match", "fail", "bug", "wrong")
_FEAT_WORDS = ("add", "introduce", "create", "support", "implement", "record")


def suggested_type(subject: str) -> str:
    """Return the Conventional Commits type that best fits a prefix-less subject.

    A guess, offered as an example for the model to change rather than as an
    answer. Two words decide it in practice - a subject about making a test
    pass is a fix, one about adding something is a feature - and anything else
    is `chore`, which is the type that claims least.
    """
    words = subject.lower()
    if any(word in words for word in _FIX_WORDS):
        return "fix"
    if any(words.startswith(word) or f" {word}" in words for word in _FEAT_WORDS):
        return "feat"
    return "chore"


def _not_conventional(subject: str, limits: ToolLimits) -> str:
    """Explain a subject with no type, and offer the model's own words with one.

    Measured rather than guessed at: on the first L1 run against a 4B, seven of
    the nine refusals that were not the benchmark's own doing were this - a
    perfectly good subject in plain English with no `type:` in front. Naming
    the rule it broke is what the message did before and it was not enough, so
    it now carries a line that can be copied.
    """
    if not subject:
        return "the commit message is empty; the first line is the subject"
    body = subject.split(":", 1)[1].strip() if ":" in subject else subject
    lowered = (body[:1].lower() + body[1:]) if body else body
    candidate = f"{suggested_type(subject)}: {lowered}"
    offer = shortened_subject(candidate, limits.max_commit_subject)
    return (
        f"subject is not Conventional Commits: {subject!r}. It needs a type and a "
        f"colon first - one of: {', '.join(COMMIT_TYPES)} - optionally with a scope, "
        f"as in `fix(gate): ...`. Use this if the type fits, or the same line with a "
        f"type that does:\n{offer}\n"
        "Then a blank line and a body saying why."
    )


class CommitMessage(BaseModel):
    """A message the gate will accept, and what the harness repaired to get it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    message: str = Field(description="What to commit, after any repair.")
    repaired: str = Field(
        default="",
        description="What was changed and why, for the tool's result. Empty when the "
        "message arrived usable.",
    )


def validate_commit_message(message: str, limits: ToolLimits) -> CommitMessage:
    """Return a message the gate will accept, shortening the subject if it must.

    Two of the three rules are judgements only the model can make, so they are
    refusals: the Conventional Commits type says what kind of change this is,
    and the body says why it was made. Neither can be invented here.

    The subject's **length** is not a judgement. It is a character count, and
    §15 says not to ask a model for bookkeeping the harness can do - the same
    rule that stopped asking for hunk headers (ADR 0022). The suggestion was
    the half-measure: this function already computed a subject that fits and
    then asked the model to apply it. A 4B was told the limit three times on a
    live run and answered 99, then 78, then 78 characters; three L3 passes then
    refused six commits apiece on length alone, with `committed_ratio` 0.0, and
    in them the refusal's own shortened subject was handed over verbatim and
    re-sent over-long anyway.

    So the harness shortens it and keeps every word the model wrote: the
    original subject becomes the body's first line, which has no limit. The
    repair is reported rather than silent, exactly as `apply_patch` reports a
    diff it repaired.

    Args:
        message: the full commit message.
        limits: caps in force for this step.

    Returns:
        The subject, the message to commit, and what was repaired.

    Raises:
        ValueError: if the subject is not a Conventional Commits subject, or
            the message has no body explaining why.
    """
    lines = message.strip().splitlines()
    subject = lines[0].strip() if lines else ""
    if not COMMIT_SUBJECT.match(subject):
        raise ValueError(_not_conventional(subject, limits))
    body = "\n".join(lines[1:]).strip()
    if not body:
        msg = "commit message has no body; the body explains why the change is made"
        raise ValueError(msg)
    if len(subject) <= limits.max_commit_subject:
        return CommitMessage(subject=subject, message=message)
    fitted = shortened_subject(subject, limits.max_commit_subject)
    over = len(subject) - limits.max_commit_subject
    return CommitMessage(
        subject=fitted,
        message=f"{fitted}\n\n{subject}\n\n{body}",
        repaired=(
            f"the subject was {len(subject)} characters, {over} over the limit, so it "
            f"was shortened to {fitted!r} and your line kept as the body's first. "
            "Counting characters is the harness's job, not yours."
        ),
    )


DANGLING_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "taken",
        "that",
        "the",
        "to",
        "using",
        "which",
        "with",
    },
)
"""Words a subject must not end on, so a truncation reads as a phrase.

Cutting at a word boundary alone produces "... the core supply taken from the",
which a model will copy verbatim. English only, which is the rule for
everything written in this repository anyway.
"""


def shortened_subject(subject: str, limit: int) -> str:
    """Return `subject` cut to `limit` at a word boundary, ending on a real word.

    Returns the subject unchanged when it already fits, and the hard
    truncation when there is no word boundary to use - a single token longer
    than the limit has nowhere better to break.
    """
    if len(subject) <= limit:
        return subject
    hard = subject[:limit]
    head = subject[: limit + 1]
    cut = head.rfind(" ")
    if cut <= 0:
        return hard
    words = head[:cut].rstrip(" ,;:-").split(" ")
    while len(words) > 1 and words[-1].lower().strip(",;:-") in DANGLING_WORDS:
        words.pop()
    candidate = " ".join(words).rstrip(" ,;:-")
    # Trimming must not cost the subject its Conventional Commits form - on
    # `fix: <one very long token>` the only word boundary is the one after
    # `fix:`, and stripping that colon offers something the next check would
    # refuse. A suggestion has to pass every rule, not just the one it is for.
    return candidate if COMMIT_SUBJECT.match(candidate) else hard


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

    refused = _argument_rules(call, args, limits)
    if refused is not None:
        return refused

    return AcceptedCall(call_id=call.call_id, tool=tool, args=args)


def _argument_rules(
    call: ToolCall,
    args: ToolArgs,
    limits: ToolLimits,
) -> RejectedCall | None:
    """Apply the per-tool argument rules, or return None when all of them pass.

    Separate from :func:`validate_call` so that the order of the general
    checks - exists, allowed, schema, path - stays readable as one list, and
    so a new tool's rule is added in one place.
    """
    if isinstance(args, ApplyPatchArgs):
        size = len(args.diff.encode("utf-8"))
        if size > limits.max_diff_bytes:
            return RejectedCall(
                call_id=call.call_id,
                name=call.name,
                violation=Violation.DIFF_TOO_LARGE,
                detail=f"diff is {size} bytes, limit is {limits.max_diff_bytes}",
            )

    if isinstance(args, ReplaceInFileArgs):
        # The same cap as a diff, because it bounds the same thing: how much
        # of a file one call may rewrite without the step being reviewed.
        size = len((args.find + args.replace).encode("utf-8"))
        if size > limits.max_diff_bytes:
            return RejectedCall(
                call_id=call.call_id,
                name=call.name,
                violation=Violation.DIFF_TOO_LARGE,
                detail=(
                    f"find and replace are {size} bytes together, limit is "
                    f"{limits.max_diff_bytes}"
                ),
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

    return None


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

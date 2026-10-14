# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool call validation: nothing unvalidated reaches the sandbox."""

import pytest

from niharness.domain.identity import CallId
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tools import (
    AcceptedCall,
    ApplyPatchArgs,
    PathConfinementError,
    ReadFileArgs,
    ToolCall,
    ToolLimits,
    ToolName,
    Violation,
    confine_path,
    truncate_result,
    validate_call,
    validate_commit_message,
)

LIMITS = ToolLimits()
IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist
REVIEWER = DEFAULT_ROLES[Role.REVIEWER].allowlist


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id=CallId("c1"), name=name, arguments=dict(arguments))


def test_accepts_a_well_formed_read() -> None:
    result = validate_call(
        call("read_file", path="src/a.py", start_line=2), IMPLEMENTER, LIMITS
    )
    assert isinstance(result, AcceptedCall)
    assert result.tool is ToolName.READ_FILE
    assert isinstance(result.args, ReadFileArgs)
    assert result.args.start_line == 2


def test_unknown_tool_is_rejected_with_the_allowlist() -> None:
    result = validate_call(call("rm_rf"), IMPLEMENTER, LIMITS)
    assert result.accepted is False
    assert result.violation is Violation.UNKNOWN_TOOL
    assert "read_file" in result.detail


def test_tool_outside_the_role_allowlist_is_rejected() -> None:
    result = validate_call(call("apply_patch", diff="--- a\n+++ b\n"), REVIEWER, LIMITS)
    assert result.accepted is False
    assert result.violation is Violation.NOT_ALLOWED


def test_shell_is_in_no_default_allowlist() -> None:
    for spec in DEFAULT_ROLES.values():
        assert ToolName.SHELL not in spec.allowlist


def test_invalid_arguments_are_rejected() -> None:
    result = validate_call(
        call("read_file", path="src/a.py", start_line=0), IMPLEMENTER, LIMITS
    )
    assert result.accepted is False
    assert result.violation is Violation.INVALID_ARGUMENTS


def test_unexpected_argument_is_rejected() -> None:
    result = validate_call(call("read_file", path="a.py", mode="rw"), IMPLEMENTER, LIMITS)
    assert result.accepted is False
    assert result.violation is Violation.INVALID_ARGUMENTS


@pytest.mark.parametrize(
    ("path", "violation"),
    [
        ("/etc/passwd", Violation.ABSOLUTE_PATH),
        ("~/.ssh/id_ed25519", Violation.ABSOLUTE_PATH),
        ("../../etc/passwd", Violation.PATH_ESCAPE),
        ("src/../../outside", Violation.PATH_ESCAPE),
    ],
)
def test_paths_are_confined_to_the_worktree(path: str, violation: Violation) -> None:
    result = validate_call(call("read_file", path=path), IMPLEMENTER, LIMITS)
    assert result.accepted is False
    assert result.violation is violation


def test_confine_path_normalises_inside_the_tree() -> None:
    assert str(confine_path("src/./sub/../a.py")) == "src/a.py"
    assert str(confine_path(".")) == "."
    with pytest.raises(PathConfinementError):
        confine_path("../x")


def test_oversized_diff_is_rejected() -> None:
    limits = ToolLimits(max_diff_bytes=64)
    result = validate_call(call("apply_patch", diff="x" * 65), IMPLEMENTER, limits)
    assert result.accepted is False
    assert result.violation is Violation.DIFF_TOO_LARGE


def test_diff_at_the_limit_is_accepted() -> None:
    limits = ToolLimits(max_diff_bytes=64)
    result = validate_call(call("apply_patch", diff="x" * 64), IMPLEMENTER, limits)
    assert isinstance(result, AcceptedCall)
    assert isinstance(result.args, ApplyPatchArgs)


@pytest.mark.parametrize(
    "message",
    [
        "add a thing\n\nbecause we need it",
        "feat: add a thing",
        "feat: " + "x" * 100 + "\n\nbody",
        "wip: add a thing\n\nbody",
    ],
)
def test_bad_commit_messages_are_rejected(message: str) -> None:
    result = validate_call(call("git_commit", message=message), IMPLEMENTER, LIMITS)
    assert result.accepted is False
    assert result.violation is Violation.COMMIT_MESSAGE


def test_conventional_commit_with_body_is_accepted() -> None:
    message = "fix(gate): stop trusting the implementer's test claims\n\nThe reviewer re-runs."
    assert validate_commit_message(message, LIMITS).startswith("fix(gate):")
    result = validate_call(call("git_commit", message=message), IMPLEMENTER, LIMITS)
    assert isinstance(result, AcceptedCall)


def test_truncation_keeps_head_and_tail() -> None:
    limits = ToolLimits(max_result_bytes=256)
    content = "start" + "m" * 5_000 + "end"
    truncated, was_truncated = truncate_result(content, limits)
    assert was_truncated
    assert truncated.startswith("start")
    assert truncated.endswith("end")
    assert "elided" in truncated
    assert len(truncated.encode()) < len(content.encode())


def test_short_results_pass_through_unchanged() -> None:
    truncated, was_truncated = truncate_result("ok", LIMITS)
    assert (truncated, was_truncated) == ("ok", False)

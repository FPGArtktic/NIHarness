# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools from servers the harness did not write.

The harness has no argument model for them, so what stands between a model and
a declared server is this: the tool was imported by name, the role was granted
it, the arguments fit the schema the server advertised, and they are not
enormous. These tests are that list, one case each.
"""

from contextlib import suppress

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from niharness.domain.identity import CallId
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tools import (
    NAMESPACE,
    AcceptedCall,
    ExternalTool,
    RejectedCall,
    SchemaMismatchError,
    ToolCall,
    ToolCatalog,
    ToolLimits,
    ToolName,
    Violation,
    check_arguments,
    is_external,
    validate_call,
)

LIMITS = ToolLimits()
IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist

REPORT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["design"],
    "properties": {
        "design": {"type": "string"},
        "stage": {"enum": ["synth", "impl"]},
        "runs": {"type": "integer"},
    },
}

REPORT = ExternalTool(
    server="fpga",
    name="read_report",
    description="Read a timing report.",
    parameters=REPORT_SCHEMA,
    roles=frozenset({"implementer", "reviewer"}),
)

SECRET = ExternalTool(
    server="fpga",
    name="program_device",
    parameters={"type": "object"},
    roles=frozenset({"reviewer"}),
)

CATALOG = ToolCatalog(external=(REPORT, SECRET))
GRANTED = IMPLEMENTER | CATALOG.allowlist_for("implementer")


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(call_id=CallId("c1"), name=name, arguments=dict(arguments))


def refusal(name: str, **arguments: object) -> RejectedCall:
    result = validate_call(call(name, **arguments), GRANTED, LIMITS, CATALOG)
    assert isinstance(result, RejectedCall)
    return result


# ------------------------------------------------------------------- addressing


def test_no_builtin_tool_can_be_mistaken_for_an_external_one() -> None:
    assert not any(is_external(tool.value) for tool in ToolName)


def test_a_server_names_its_own_namespace() -> None:
    assert REPORT.id == f"fpga{NAMESPACE}read_report"
    assert CATALOG.get(REPORT.id) is REPORT
    assert CATALOG.get("fpga__nothing") is None


def test_a_catalogue_reports_what_each_server_contributed() -> None:
    assert CATALOG.servers() == ("fpga",)
    assert CATALOG.tools_of("fpga") == ("read_report", "program_device")
    assert CATALOG.roles_of("fpga") == ("implementer", "reviewer")


# -------------------------------------------------------------------- permission


def test_a_granted_tool_is_accepted_with_the_arguments_as_advertised() -> None:
    result = validate_call(
        call("fpga__read_report", design="top", stage="synth"),
        GRANTED,
        LIMITS,
        CATALOG,
    )
    assert isinstance(result, AcceptedCall)
    assert result.tool == "fpga__read_report"
    assert result.args.model_dump(mode="json") == {"design": "top", "stage": "synth"}


def test_a_tool_this_role_was_not_granted_is_refused() -> None:
    assert refusal("fpga__program_device").violation is Violation.NOT_ALLOWED


def test_a_tool_no_server_imported_is_unknown() -> None:
    rejected = refusal("other__exfiltrate")
    assert rejected.violation is Violation.UNKNOWN_TOOL
    assert "fpga__read_report" in rejected.detail


def test_without_a_catalogue_every_namespaced_name_is_unknown() -> None:
    result = validate_call(call("fpga__read_report", design="top"), GRANTED, LIMITS)
    assert isinstance(result, RejectedCall)
    assert result.violation is Violation.UNKNOWN_TOOL


# ----------------------------------------------------------------------- shape


def test_a_missing_required_argument_is_refused() -> None:
    rejected = refusal("fpga__read_report", stage="synth")
    assert rejected.violation is Violation.INVALID_ARGUMENTS
    assert "missing design" in rejected.detail


def test_an_argument_of_the_wrong_type_is_refused() -> None:
    rejected = refusal("fpga__read_report", design=7)
    assert rejected.violation is Violation.INVALID_ARGUMENTS
    assert "the schema says string" in rejected.detail


def test_an_argument_outside_the_enumeration_is_refused() -> None:
    assert refusal("fpga__read_report", design="top", stage="route").violation is (
        Violation.INVALID_ARGUMENTS
    )


def test_an_argument_the_schema_closes_the_door_on_is_refused() -> None:
    rejected = refusal("fpga__read_report", design="top", bitstream="/dev/sda")
    assert rejected.violation is Violation.INVALID_ARGUMENTS
    assert "does not take bitstream" in rejected.detail


def test_arguments_larger_than_the_cap_are_refused() -> None:
    limits = ToolLimits(max_argument_bytes=256)
    result = validate_call(
        call("fpga__read_report", design="x" * 1024),
        GRANTED,
        limits,
        CATALOG,
    )
    assert isinstance(result, RejectedCall)
    assert result.violation is Violation.ARGUMENTS_TOO_LARGE


def test_a_boolean_is_not_an_integer() -> None:
    with pytest.raises(SchemaMismatchError):
        check_arguments({"type": "integer"}, True)


def test_a_nested_value_is_named_by_where_it_sits() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"ports": {"type": "array", "items": {"type": "string"}}},
    }
    with pytest.raises(SchemaMismatchError, match=r"arguments\.ports\[1\]"):
        check_arguments(schema, {"ports": ["clk", 3]})


def test_a_schema_the_harness_does_not_understand_is_left_to_the_server() -> None:
    # Combinators and formats are not checked here; the call goes through and
    # the server remains the authority on its own arguments.
    check_arguments({"oneOf": [{"type": "string"}], "format": "uri"}, {"anything": 1})


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
        lambda children: st.lists(children) | st.dictionaries(st.text(), children),
        max_leaves=12,
    ),
)
def test_the_check_never_fails_in_any_other_way(value: JsonValue) -> None:
    # Arguments come from a model, so they are arbitrary JSON. Whatever they
    # are, the answer is "fine" or one readable refusal - never a crash the
    # step would have to survive.
    with suppress(SchemaMismatchError):
        check_arguments(REPORT_SCHEMA, value)

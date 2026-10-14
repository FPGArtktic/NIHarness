# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Role definitions: permission is per role, and read-only means read-only."""

from niharness.domain.roles import DEFAULT_ROLES, POLICY_LAYER, ModelKind, Role, spec_for
from niharness.domain.tools import READ_ONLY_TOOLS, ToolName
from niharness.prompts import available_layers


def test_every_role_is_defined() -> None:
    assert set(DEFAULT_ROLES) == set(Role)


def test_policy_is_the_first_layer_of_every_role() -> None:
    for spec in DEFAULT_ROLES.values():
        assert spec.prompt_layers[0] == POLICY_LAYER


def test_every_referenced_layer_ships_with_the_package() -> None:
    shipped = set(available_layers())
    for spec in DEFAULT_ROLES.values():
        assert set(spec.prompt_layers) <= shipped, spec.role


def test_read_only_roles_cannot_change_anything() -> None:
    for role in (Role.BRAINSTORMER, Role.PLANNER, Role.RESEARCHER):
        assert spec_for(role).allowlist <= READ_ONLY_TOOLS


def test_the_reviewer_can_run_the_checks_but_not_patch_or_commit() -> None:
    allowlist = spec_for(Role.REVIEWER).allowlist
    assert {ToolName.RUN_BUILD, ToolName.RUN_TESTS} <= allowlist
    assert ToolName.APPLY_PATCH not in allowlist
    assert ToolName.GIT_COMMIT not in allowlist


def test_only_implementer_and_fixer_may_commit() -> None:
    committers = {
        role for role, spec in DEFAULT_ROLES.items() if ToolName.GIT_COMMIT in spec.allowlist
    }
    assert committers == {Role.IMPLEMENTER, Role.FIXER}


def test_write_access_always_comes_as_a_diff() -> None:
    for spec in DEFAULT_ROLES.values():
        if ToolName.APPLY_PATCH in spec.allowlist:
            assert ToolName.SHELL not in spec.allowlist


def test_the_compactor_runs_on_the_utility_model_without_tools() -> None:
    spec = spec_for(Role.COMPACTOR)
    assert spec.model_kind is ModelKind.UTILITY
    assert not spec.uses_tools
    assert spec.allowlist == frozenset()
    assert spec.structured_output == "ContextSummary"


def test_brainstorming_is_the_only_warm_role() -> None:
    temperatures = {role: spec.temperature for role, spec in DEFAULT_ROLES.items()}
    assert temperatures[Role.BRAINSTORMER] == max(temperatures.values())
    assert temperatures[Role.REVIEWER] == 0.0

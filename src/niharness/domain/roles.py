# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Roles.

A role is a prompt stack, a tool allowlist, a temperature and a model kind.
Inside a run there is exactly one main model, so roles never cause a model
swap: they differ by prompt and permission, not by weights. A swap between the
Thinking and the Instruct variant is allowed only at a stage boundary.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.tools import READ_ONLY_TOOLS, ToolName


class Role(StrEnum):
    """The agent roles the pipeline runs."""

    BRAINSTORMER = "brainstormer"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    FIXER = "fixer"
    RESEARCHER = "researcher"
    COMPACTOR = "compactor"


class ModelKind(StrEnum):
    """Which resident model serves a role."""

    MAIN = "main"
    """The coder model: plan, implement, review, fix, research."""

    THINKING = "thinking"
    """The reasoning variant: brainstorm, and optionally plan and review."""

    UTILITY = "utility"
    """The ternary model on CPU: summaries, compaction, NOTES.md, routing."""


POLICY_LAYER = "00-policy.md"
"""The immutable first prompt layer; it is part of every role's stack."""

DOD_LAYER = "20-checklist-dod.md"
"""The Definition of Done checklist, appended for roles that must finish something."""


class RoleSpec(BaseModel):
    """Everything that distinguishes one role from another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Role
    model_kind: ModelKind
    prompt_layers: tuple[str, ...] = Field(min_length=1)
    allowlist: frozenset[ToolName]
    temperature: float = Field(ge=0.0, le=2.0)
    uses_tools: bool = Field(description="False means a single-shot step, not a ReAct loop.")
    structured_output: str | None = Field(
        default=None,
        description="Name of the artifact model the response must validate against.",
    )
    log_reasoning: bool = Field(
        default=True,
        description="Reasoning is logged and shown; it is never fed back into the history.",
    )


def _layers(role: Role, *, dod: bool = True) -> tuple[str, ...]:
    """Return the prompt stack of `role`: policy, role layer, optionally the checklist."""
    stack = [POLICY_LAYER, f"10-role-{role.value}.md"]
    if dod:
        stack.append(DOD_LAYER)
    return tuple(stack)


_IMPLEMENTER_TOOLS: frozenset[ToolName] = READ_ONLY_TOOLS | {
    ToolName.REPLACE_IN_FILE,
    ToolName.APPLY_PATCH,
    ToolName.FORMAT_CODE,
    ToolName.RUN_LINT,
    ToolName.RUN_BUILD,
    ToolName.RUN_TESTS,
    ToolName.GIT_COMMIT,
    ToolName.ADD_DEPENDENCY,
}

_REVIEWER_TOOLS: frozenset[ToolName] = READ_ONLY_TOOLS | {
    ToolName.RUN_LINT,
    ToolName.RUN_BUILD,
    ToolName.RUN_TESTS,
}

DEFAULT_ROLES: dict[Role, RoleSpec] = {
    Role.BRAINSTORMER: RoleSpec(
        role=Role.BRAINSTORMER,
        model_kind=ModelKind.THINKING,
        prompt_layers=_layers(Role.BRAINSTORMER, dod=False),
        allowlist=READ_ONLY_TOOLS,
        temperature=0.8,
        uses_tools=True,
        structured_output=None,
    ),
    Role.PLANNER: RoleSpec(
        role=Role.PLANNER,
        model_kind=ModelKind.MAIN,
        prompt_layers=_layers(Role.PLANNER, dod=False),
        allowlist=READ_ONLY_TOOLS,
        temperature=0.2,
        uses_tools=True,
        structured_output="Plan",
    ),
    Role.IMPLEMENTER: RoleSpec(
        role=Role.IMPLEMENTER,
        model_kind=ModelKind.MAIN,
        prompt_layers=_layers(Role.IMPLEMENTER),
        allowlist=frozenset(_IMPLEMENTER_TOOLS),
        temperature=0.1,
        uses_tools=True,
        structured_output="FinalAnswer",
    ),
    Role.REVIEWER: RoleSpec(
        role=Role.REVIEWER,
        model_kind=ModelKind.MAIN,
        prompt_layers=_layers(Role.REVIEWER, dod=False),
        allowlist=frozenset(_REVIEWER_TOOLS),
        temperature=0.0,
        uses_tools=True,
        structured_output="Verdict",
    ),
    Role.FIXER: RoleSpec(
        role=Role.FIXER,
        model_kind=ModelKind.MAIN,
        prompt_layers=_layers(Role.FIXER),
        allowlist=frozenset(_IMPLEMENTER_TOOLS),
        temperature=0.1,
        uses_tools=True,
        structured_output="FinalAnswer",
    ),
    Role.RESEARCHER: RoleSpec(
        role=Role.RESEARCHER,
        model_kind=ModelKind.MAIN,
        prompt_layers=_layers(Role.RESEARCHER, dod=False),
        allowlist=READ_ONLY_TOOLS,
        temperature=0.3,
        uses_tools=True,
        structured_output=None,
    ),
    Role.COMPACTOR: RoleSpec(
        role=Role.COMPACTOR,
        model_kind=ModelKind.UTILITY,
        prompt_layers=_layers(Role.COMPACTOR, dod=False),
        allowlist=frozenset(),
        temperature=0.0,
        uses_tools=False,
        structured_output="ContextSummary",
        log_reasoning=False,
    ),
}
"""The shipped role definitions. `harness.toml` may override temperature and budgets."""


def spec_for(role: Role) -> RoleSpec:
    """Return the shipped specification of `role`."""
    return DEFAULT_ROLES[role]

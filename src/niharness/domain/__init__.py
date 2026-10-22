# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The domain: pipeline, contracts, gate logic, loop detectors, compaction policy.

Nothing in this package performs I/O, reads a clock, or imports an adapter.
Everything is a pure function or a frozen model, so the interesting decisions
of the harness are unit-testable without a model, a container or a filesystem.
The architecture check (`uv run lint-imports`) enforces that.
"""

from niharness.domain.artifacts import (
    AcceptanceCriterion,
    Answer,
    ContextSummary,
    Decision,
    FinalAnswer,
    Plan,
    Question,
    Questions,
    ReviewNote,
    Severity,
    Spec,
    Task,
    Verdict,
)
from niharness.domain.budgets import Budgets, BudgetState
from niharness.domain.context import CompactionPolicy, ContextItem, Priority, plan_compaction
from niharness.domain.events import Event, InterventionLevel, StopReason
from niharness.domain.gate import GateResult, GateTier, Language, plan_gate
from niharness.domain.identity import RunId, StepId
from niharness.domain.loop import LoopThresholds, Pathology, evaluate
from niharness.domain.pipeline import PIPELINE, Outcome, Stage, advance
from niharness.domain.roles import DEFAULT_ROLES, Role, RoleSpec
from niharness.domain.state import RunSnapshot, fold
from niharness.domain.tool_loop import LoopConfig, LoopState, next_action, start
from niharness.domain.tools import (
    EMPTY_CATALOG,
    ExternalTool,
    ToolCatalog,
    ToolName,
    validate_call,
)

__all__ = [
    "DEFAULT_ROLES",
    "EMPTY_CATALOG",
    "PIPELINE",
    "AcceptanceCriterion",
    "Answer",
    "BudgetState",
    "Budgets",
    "CompactionPolicy",
    "ContextItem",
    "ContextSummary",
    "Decision",
    "Event",
    "ExternalTool",
    "FinalAnswer",
    "GateResult",
    "GateTier",
    "InterventionLevel",
    "Language",
    "LoopConfig",
    "LoopState",
    "LoopThresholds",
    "Outcome",
    "Pathology",
    "Plan",
    "Priority",
    "Question",
    "Questions",
    "ReviewNote",
    "Role",
    "RoleSpec",
    "RunId",
    "RunSnapshot",
    "Severity",
    "Spec",
    "Stage",
    "StepId",
    "StopReason",
    "Task",
    "ToolCatalog",
    "ToolName",
    "Verdict",
    "advance",
    "evaluate",
    "fold",
    "next_action",
    "plan_compaction",
    "plan_gate",
    "start",
    "validate_call",
]

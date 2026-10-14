# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Budgets.

Budgets are enforced by the harness, never requested from the model. A step
carries a tool-call budget, a token budget and a wall-time budget; when any of
them runs out the step stops with a recorded reason.

Shortly before the tool-call budget runs out the harness forces finalisation:
the next model call is made with `tool_choice: none`, so the step ends with a
written state in NOTES.md instead of a truncated tool call.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.events import StopReason

FINALIZATION_MARGIN = 2
"""How many tool calls before the limit the harness stops offering tools."""


class Budgets(BaseModel):
    """The limits of one step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(default=40, ge=0)
    max_tokens: int = Field(default=120_000, ge=0)
    max_wall_seconds: int = Field(default=1_800, ge=0)
    finalization_margin: int = Field(default=FINALIZATION_MARGIN, ge=0)


class BudgetState(BaseModel):
    """What a step has spent so far. Immutable: spending returns a new state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    budgets: Budgets
    tool_calls_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    wall_seconds_used: float = Field(default=0.0, ge=0)

    def spend(
        self,
        *,
        tool_calls: int = 0,
        tokens: int = 0,
        wall_seconds: float = 0.0,
    ) -> BudgetState:
        """Return a new state with the given amounts added."""
        return self.model_copy(
            update={
                "tool_calls_used": self.tool_calls_used + tool_calls,
                "tokens_used": self.tokens_used + tokens,
                "wall_seconds_used": self.wall_seconds_used + wall_seconds,
            },
        )

    @property
    def tool_calls_remaining(self) -> int:
        """Tool calls left before the budget is exhausted."""
        return max(0, self.budgets.max_tool_calls - self.tool_calls_used)

    @property
    def must_finalize(self) -> bool:
        """True when the next model call must be made without tools."""
        return self.tool_calls_remaining <= self.budgets.finalization_margin

    def exhausted(self) -> StopReason | None:
        """Return the stop reason if a budget ran out, otherwise None."""
        if self.tool_calls_used >= self.budgets.max_tool_calls:
            return StopReason.BUDGET_TOOL_CALLS
        if self.tokens_used >= self.budgets.max_tokens:
            return StopReason.BUDGET_TOKENS
        if self.wall_seconds_used >= self.budgets.max_wall_seconds:
            return StopReason.BUDGET_WALL_TIME
        return None

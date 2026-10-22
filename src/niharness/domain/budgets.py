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

MIN_FITTED_TOKENS = 2_000
"""The smallest token budget a fitted step keeps.

Below this a step cannot read a window and write an answer, so the honest
outcome is a step that runs and fails on its budget - with its reason in the
log - rather than one quietly reduced to nothing.
"""


class Budgets(BaseModel):
    """The limits of one step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(default=40, ge=0)
    max_tokens: int = Field(default=120_000, ge=0)
    max_wall_seconds: int = Field(default=1_800, ge=0)
    finalization_margin: int = Field(default=FINALIZATION_MARGIN, ge=0)


def fit_to_host(budgets: Budgets, generation_tps: float) -> Budgets:
    """Return `budgets` with the token cap the wall-clock budget can actually pay for.

    The shipped budgets are a guess about a host. Tokens are the real resource
    and wall time is tokens divided by throughput, so on a measured host the
    two have to agree. They did not: an implement step is allowed 120k tokens
    in 1800 seconds, which needs 67 tokens a second. Measured on a laptop CPU,
    Bonsai 2 27B generates 1.66 - so the step would have been cut off by the
    clock at about one fortieth of its token budget, with nothing written down,
    because forced finalisation watches the tool budget and not the clock.

    Lowering the token cap to what the clock can pay for makes the step end the
    way it is designed to end: the tool budget runs out on time, finalisation
    fires, and the step writes its state. The wall-clock budget is left alone -
    it is the guard against a hang, and a guard that moves with the thing it
    guards is not a guard.

    Args:
        budgets: the step's shipped budgets.
        generation_tps: measured generation throughput, from the L0 benchmark.
            Zero or negative means unmeasured, and then nothing is changed:
            a guess is not improved by scaling it with another guess.

    Returns:
        The budgets this host can keep.
    """
    if generation_tps <= 0 or budgets.max_wall_seconds <= 0:
        return budgets
    affordable = int(budgets.max_wall_seconds * generation_tps)
    if affordable >= budgets.max_tokens:
        return budgets
    return budgets.model_copy(update={"max_tokens": max(MIN_FITTED_TOKENS, affordable)})


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

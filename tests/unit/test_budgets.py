# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Budgets are enforced by the harness, including the forced finalisation."""

from niharness.domain.budgets import Budgets, BudgetState
from niharness.domain.events import StopReason


def state(**kwargs: int) -> BudgetState:
    return BudgetState(
        budgets=Budgets(max_tool_calls=10, max_tokens=1_000, max_wall_seconds=60),
        **kwargs,
    )


def test_spending_returns_a_new_state() -> None:
    first = state()
    second = first.spend(tool_calls=1, tokens=100, wall_seconds=2.5)
    assert first.tool_calls_used == 0
    assert (second.tool_calls_used, second.tokens_used, second.wall_seconds_used) == (
        1,
        100,
        2.5,
    )


def test_nothing_is_exhausted_at_the_start() -> None:
    assert state().exhausted() is None


def test_each_budget_reports_its_own_stop_reason() -> None:
    assert state(tool_calls_used=10).exhausted() is StopReason.BUDGET_TOOL_CALLS
    assert state(tokens_used=1_000).exhausted() is StopReason.BUDGET_TOKENS
    assert state(wall_seconds_used=60).exhausted() is StopReason.BUDGET_WALL_TIME


def test_tool_calls_are_exhausted_before_tokens_are_reported() -> None:
    both = state(tool_calls_used=10, tokens_used=1_000)
    assert both.exhausted() is StopReason.BUDGET_TOOL_CALLS


def test_finalisation_is_forced_two_calls_before_the_limit() -> None:
    assert not state(tool_calls_used=7).must_finalize
    assert state(tool_calls_used=8).must_finalize
    assert state(tool_calls_used=10).must_finalize


def test_remaining_never_goes_negative() -> None:
    assert state(tool_calls_used=99).tool_calls_remaining == 0

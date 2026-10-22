# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Budgets are enforced by the harness, including the forced finalisation."""

from niharness.domain.budgets import MIN_FITTED_TOKENS, Budgets, BudgetState, fit_to_host
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


# ----------------------------------------------- budgets and the host they run on


def test_a_measured_host_gets_the_token_budget_its_clock_can_pay_for() -> None:
    # The shipped implement budget needs 67 tokens a second to spend its tokens
    # inside its half hour. Measured on this laptop's CPU, Bonsai 2 27B does
    # 1.66, so the step would have been stopped by the clock at a fortieth of
    # its token budget - and forced finalisation watches the tool budget, not
    # the clock, so nothing would have been written down.
    shipped = Budgets(max_tool_calls=25, max_tokens=120_000, max_wall_seconds=1_800)
    fitted = fit_to_host(shipped, 1.66)
    assert fitted.max_tokens == int(1_800 * 1.66)
    assert fitted.max_wall_seconds == shipped.max_wall_seconds, "the guard does not move"
    assert fitted.max_tool_calls == shipped.max_tool_calls


def test_a_fast_host_keeps_the_budget_it_was_given() -> None:
    shipped = Budgets(max_tool_calls=25, max_tokens=120_000, max_wall_seconds=1_800)
    assert fit_to_host(shipped, 200.0) == shipped


def test_an_unmeasured_host_is_left_alone() -> None:
    # A guess is not improved by scaling it with another guess.
    shipped = Budgets(max_tokens=120_000, max_wall_seconds=1_800)
    assert fit_to_host(shipped, 0.0) == shipped
    assert fit_to_host(shipped, -1.0) == shipped


def test_a_hopeless_host_still_gets_enough_to_fail_with_a_reason() -> None:
    # At a tenth of a token a second the arithmetic says 180 tokens, which
    # cannot read a window or write an answer. The floor keeps the step real,
    # so it runs and stops on its budget with its reason in the log, instead
    # of being quietly reduced to nothing.
    fitted = fit_to_host(Budgets(max_tokens=120_000, max_wall_seconds=1_800), 0.1)
    assert fitted.max_tokens == MIN_FITTED_TOKENS


def test_a_step_with_no_clock_budget_is_not_fitted() -> None:
    unbounded = Budgets(max_tokens=10_000, max_wall_seconds=0)
    assert fit_to_host(unbounded, 1.0) == unbounded

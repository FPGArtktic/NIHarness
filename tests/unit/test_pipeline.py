# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pipeline transitions, including the bounds that hand work back to the human."""

import pytest

from niharness.domain.pipeline import (
    FRESH_RUN_STAGES,
    PIPELINE,
    TERMINAL_STAGES,
    Outcome,
    Stage,
    advance,
    step_for,
)
from niharness.domain.roles import DEFAULT_ROLES


def test_spec_acceptance_starts_a_fresh_run() -> None:
    transition = advance(Stage.SPEC_GATE, Outcome.GREEN)
    assert transition.stage is Stage.PLAN
    assert transition.fresh_run
    assert transition.stage in FRESH_RUN_STAGES


def test_approval_starts_a_fresh_run_for_implementation() -> None:
    transition = advance(Stage.APPROVAL, Outcome.APPROVED)
    assert (transition.stage, transition.fresh_run, transition.revision) == (
        Stage.IMPLEMENT,
        True,
        0,
    )


def test_red_quick_gate_goes_to_fix_and_counts_a_revision() -> None:
    transition = advance(Stage.COMMIT_GATE, Outcome.RED, revision=0)
    assert transition.stage is Stage.FIX
    assert transition.revision == 1


def test_fix_loop_limit_hands_the_task_back() -> None:
    transition = advance(Stage.REVIEW, Outcome.NAK, revision=3, max_revisions=3)
    assert transition.stage is Stage.HANDOFF
    assert transition.terminal
    assert "fix loop limit" in transition.reason


def test_ack_goes_to_the_full_gate_then_completes() -> None:
    assert advance(Stage.REVIEW, Outcome.ACK).stage is Stage.FULL_GATE
    assert advance(Stage.FULL_GATE, Outcome.GREEN).stage is Stage.COMPLETED


def test_questions_pause_the_stage_without_moving() -> None:
    transition = advance(Stage.PLAN, Outcome.QUESTIONS, revision=2)
    assert transition.stage is Stage.PLAN
    assert transition.revision == 2
    assert not transition.terminal


def test_failure_anywhere_is_a_handoff() -> None:
    for stage in (Stage.PLAN, Stage.IMPLEMENT, Stage.REVIEW):
        assert advance(stage, Outcome.FAILED).stage is Stage.HANDOFF


def test_unexpected_outcome_is_a_handoff_not_a_crash() -> None:
    transition = advance(Stage.IMPLEMENT, Outcome.ACK)
    assert transition.stage is Stage.HANDOFF
    assert "unexpected outcome" in transition.reason


def test_implement_and_fix_both_lead_to_the_commit_gate() -> None:
    assert advance(Stage.IMPLEMENT, Outcome.DONE).stage is Stage.COMMIT_GATE
    assert advance(Stage.FIX, Outcome.DONE).stage is Stage.COMMIT_GATE


def test_every_non_terminal_stage_has_a_step_definition() -> None:
    for stage in Stage:
        if stage in TERMINAL_STAGES:
            with pytest.raises(KeyError):
                step_for(stage)
        else:
            assert step_for(stage).stage is stage


def test_steps_with_a_role_use_a_defined_role() -> None:
    for step in PIPELINE:
        if step.role is not None:
            assert step.role in DEFAULT_ROLES


def test_budgets_are_zero_for_stages_without_a_model() -> None:
    for step in PIPELINE:
        if step.role is None:
            assert step.budgets.max_tool_calls == 0
            assert step.budgets.max_tokens == 0

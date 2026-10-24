# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Detectors fire on synthetic event windows and are deterministic."""

from hypothesis import given
from hypothesis import strategies as st

from niharness.domain.events import InterventionLevel
from niharness.domain.identity import Digest
from niharness.domain.loop import (
    CallRecord,
    LoopFinding,
    LoopThresholds,
    Pathology,
    degenerate_output,
    evaluate,
    next_level,
    ngram_repeats,
    synthetic_result,
)

THRESHOLDS = LoopThresholds()


def record(
    seq: int,
    tool: str = "read_file",
    args: str = "a",
    result: str | None = "r",
    tree: str | None = "t",
    *,
    invalid: bool = False,
    gate: str = "",
) -> CallRecord:
    return CallRecord(
        seq=seq,
        tool=tool,
        arguments_digest=Digest(args),
        result_digest=Digest(result) if result else None,
        tree_digest=Digest(tree) if tree else None,
        invalid=invalid,
        gate_signature=gate,
    )


def pathologies(window: list[CallRecord]) -> set[Pathology]:
    return {finding.pathology for finding in evaluate(window, THRESHOLDS)}


def test_empty_window_fires_nothing() -> None:
    assert evaluate([], THRESHOLDS) == ()


def test_exact_repeat_fires_on_identical_arguments() -> None:
    window = [record(1), record(2)]
    assert Pathology.EXACT_REPEAT in pathologies(window)


def test_same_tool_with_different_arguments_is_not_a_repeat() -> None:
    window = [record(1, args="a"), record(2, args="b"), record(3, args="c")]
    assert Pathology.EXACT_REPEAT not in pathologies(window)


def test_repeat_outside_the_window_is_forgotten() -> None:
    window = [record(1, args="a"), *[record(index, args=f"x{index}") for index in range(2, 8)]]
    assert Pathology.EXACT_REPEAT not in pathologies(window)


def test_state_cycle_fires_when_the_tree_comes_back() -> None:
    window = [
        record(1, args="1", tree="A"),
        record(2, args="2", tree="B"),
        record(3, args="3", tree="A"),
    ]
    assert Pathology.STATE_CYCLE in pathologies(window)


def test_monotone_progress_is_not_a_cycle() -> None:
    window = [record(index, args=str(index), tree=f"T{index}") for index in range(1, 6)]
    assert Pathology.STATE_CYCLE not in pathologies(window)


def test_progress_stall_needs_both_no_change_and_a_known_result() -> None:
    window = [record(index, args=str(index), result="same", tree="T") for index in range(1, 11)]
    findings = {finding.pathology: finding for finding in evaluate(window, THRESHOLDS)}
    assert Pathology.PROGRESS_STALL in findings
    assert findings[Pathology.PROGRESS_STALL].observed >= THRESHOLDS.progress_stall_calls


def test_new_results_are_progress_even_without_tree_changes() -> None:
    window = [
        record(index, args=str(index), result=f"r{index}", tree="T") for index in range(1, 12)
    ]
    assert Pathology.PROGRESS_STALL not in pathologies(window)


def test_gate_thrash_counts_identical_failure_signatures() -> None:
    window = [record(index, args=str(index), gate="sig-1") for index in range(1, 4)]
    assert Pathology.GATE_THRASH in pathologies(window)


def test_different_gate_failures_are_not_thrash() -> None:
    window = [record(index, args=str(index), gate=f"sig-{index}") for index in range(1, 6)]
    assert Pathology.GATE_THRASH not in pathologies(window)


def test_invalid_calls_must_be_consecutive() -> None:
    broken = [record(index, args=str(index), invalid=True) for index in range(1, 4)]
    assert Pathology.INVALID_CALLS in pathologies(broken)
    interrupted = [*broken[:2], record(9, args="ok"), broken[2]]
    assert Pathology.INVALID_CALLS not in pathologies(interrupted)


def test_findings_are_ordered_most_specific_first() -> None:
    window = [
        record(index, args="same", result="same", tree="T", invalid=True, gate="sig")
        for index in range(1, 12)
    ]
    order = [finding.pathology for finding in evaluate(window, THRESHOLDS)]
    assert order[0] is Pathology.INVALID_CALLS
    assert order == sorted(order, key=lambda item: order.index(item))


@given(
    st.lists(
        st.builds(
            record,
            st.integers(min_value=1, max_value=50),
            st.sampled_from(["read_file", "grep", "run_tests"]),
            st.sampled_from(["a", "b"]),
            st.sampled_from(["r1", "r2", None]),
            st.sampled_from(["T1", "T2", None]),
        ),
        max_size=12,
    ),
)
def test_same_window_always_yields_the_same_verdict(window: list[CallRecord]) -> None:
    assert evaluate(window, THRESHOLDS) == evaluate(window, THRESHOLDS)


def test_degenerate_output_detects_repetition() -> None:
    text = " ".join(["the same eight words repeated over and over"] * 6)
    finding = degenerate_output(text, THRESHOLDS)
    assert finding is not None
    assert finding.pathology is Pathology.DEGENERATE_OUTPUT


def test_normal_prose_is_not_degenerate() -> None:
    text = "The reviewer re-runs the build itself because the implementer may be wrong."
    assert degenerate_output(text, THRESHOLDS) is None


def test_ngram_counting_handles_short_text() -> None:
    assert ngram_repeats("too short", 8) == 0


def test_ladder_climbs_one_rung_per_firing() -> None:
    findings = list(evaluate([record(1), record(2)], THRESHOLDS))
    assert next_level([], findings) is InterventionLevel.SYNTHETIC_RESULT
    assert next_level([InterventionLevel.SYNTHETIC_RESULT], findings) is (
        InterventionLevel.REFLECTION_TURN
    )
    assert (
        next_level(
            [InterventionLevel.SYNTHETIC_RESULT, InterventionLevel.REFLECTION_TURN],
            findings,
        )
        is InterventionLevel.ROLLBACK_RESTART
    )


def test_ladder_stops_at_step_failure() -> None:
    findings = list(evaluate([record(1), record(2)], THRESHOLDS))
    assert (
        next_level([InterventionLevel.STEP_FAILURE], findings) is InterventionLevel.STEP_FAILURE
    )


def test_ladder_does_nothing_without_findings() -> None:
    assert next_level([InterventionLevel.SYNTHETIC_RESULT], []) is None


def test_synthetic_result_names_the_tool_and_offers_a_way_out() -> None:
    text = synthetic_result("read_file", "read_file src/a.py: 12 lines, sha abc")
    assert "read_file" in text
    assert "NOTES.md" in text


def test_a_repeated_refusal_is_quoted_back_not_hashed() -> None:
    """The refusal's words are the one thing that would help, and they were dropped.

    This used to interpolate a hex digest - "Digest of the previous result:
    3f9a..." - at exactly the moment a model most needed the reason repeated.
    Measured: on the 20-task L1 suite every one of nine failing tasks ended
    with the same two interventions, and the pattern was a tool refusing, the
    model re-sending the identical call, and the loop stopping it.
    """
    reason = (
        "that passage is not in the file. The closest line in it is:\n"
        "```\n    def reset(self):\n```"
    )
    text = synthetic_result("replace_in_file", reason, failed=True)
    assert "already refused" in text
    assert "The closest line in it is" in text, "its own words, not a description of them"
    assert "Digest" not in text
    assert "Repeating it will keep failing" in text


def test_a_repeated_success_still_gets_the_short_form() -> None:
    """Nothing is wrong with the result, so there is nothing to quote."""
    text = synthetic_result("read_file", "3f9adc12", failed=False)
    assert "already executed" in text
    assert "Digest of the previous result: 3f9adc12" in text


def test_a_very_long_refusal_is_cut_rather_than_resent_whole() -> None:
    text = synthetic_result("run_tests", "x" * 10_000, failed=True)
    assert len(text) < 2_000
    assert "[cut]" in text


def test_a_step_that_writes_nothing_never_rolls_back() -> None:
    """Level three resets the worktree and restarts the step with a summary.

    For an implementer that is a way out of a mess it made. A reviewer made no
    mess - there is nothing to roll back - and the restart throws away
    everything it read. Measured on L2: a reviewer reached level three,
    decided its case correctly, and anchored its note nowhere, with
    `located_ratio` 0.0 for that pass.
    """
    fired = (
        LoopFinding(
            pathology=Pathology.EXACT_REPEAT,
            observed=2,
            threshold=2,
            evidence="read_file called twice with identical arguments",
        ),
    )
    climbed = [InterventionLevel.SYNTHETIC_RESULT, InterventionLevel.REFLECTION_TURN]

    assert next_level(climbed, fired) is InterventionLevel.ROLLBACK_RESTART
    assert next_level(climbed, fired, writes=True) is InterventionLevel.ROLLBACK_RESTART
    assert next_level(climbed, fired, writes=False) is InterventionLevel.STEP_FAILURE

    # The rungs below it are the same either way: a repeat message and a
    # reflection cost a read-only step nothing it cannot afford.
    assert next_level([], fired, writes=False) is InterventionLevel.SYNTHETIC_RESULT
    assert (
        next_level([InterventionLevel.SYNTHETIC_RESULT], fired, writes=False)
        is InterventionLevel.REFLECTION_TURN
    )

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoring a review against the verdict a human gave the same change."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from niharness.cli.bench import load_l2, stage_case
from niharness.domain.artifacts import Decision, ReviewNote, Severity, Verdict
from niharness.domain.bench import (
    CaseScore,
    L2Case,
    score_case,
    score_reviews,
)
from niharness.domain.events import (
    InterventionLevel,
    LoopIntervention,
    ModelResponse,
    Payload,
    StepFinished,
    StopReason,
    ToolCalled,
    ToolReturned,
)
from niharness.domain.identity import CallId, Digest, digest_json

ROOT = Path(__file__).resolve().parents[2]


def nak_case(**overrides: object) -> L2Case:
    fields: dict[str, object] = {
        "id": "swallows-a-bad-reading",
        "title": "Keep averaging when one line cannot be parsed",
        "intent": "one bad reading should not stop a run",
        "verdict": Decision.NAK,
        "defect_files": ("src/sampler.py",),
        "mentions_any": ("LOWER_BOUND", "except"),
        "why": "garbage counts as the bottom of the band",
    }
    return L2Case.model_validate(fields | overrides)


def ack_case(**overrides: object) -> L2Case:
    fields: dict[str, object] = {
        "id": "a-helper-extracted",
        "title": "Extract the in-band filter",
        "intent": "a caller had to repeat the filter",
        "verdict": Decision.ACK,
    }
    return L2Case.model_validate(fields | overrides)


def verdict(
    decision: Decision,
    *,
    summary: str = "the error is discarded",
    files: tuple[str, ...] = ("src/sampler.py",),
    message: str = "the except clause substitutes LOWER_BOUND for a bad reading",
    tests_passed: bool = True,
) -> Verdict:
    return Verdict(
        decision=decision,
        summary=summary,
        build_passed=True,
        tests_passed=tests_passed,
        notes=(
            ()
            if decision is Decision.ACK
            else tuple(
                ReviewNote(file=name, line=24, severity=Severity.BLOCKER, message=message)
                for name in files
            )
        ),
    )


def ran_the_tests(*, green: bool = True, seconds: float = 0.0) -> tuple[Payload, ...]:
    call = CallId("c1")
    return (
        ToolCalled(
            call_id=call,
            name="run_tests",
            arguments={},
            arguments_digest=digest_json({}),
        ),
        ToolReturned(
            call_id=call,
            ok=green,
            result_digest=digest_json({"out": green}),
            truncated=False,
            summary="4 passed" if green else "1 failed",
            duration_seconds=seconds,
        ),
        StepFinished(
            stop_reason=StopReason.FINAL_ANSWER,
            tool_calls_used=1,
            tokens_used=800,
            wall_seconds=12.0,
        ),
    )


# ------------------------------------------------------------------ the answer key


def test_a_nak_case_has_to_say_where_and_why() -> None:
    with pytest.raises(ValueError, match="defect_files and mentions_any"):
        nak_case(defect_files=(), mentions_any=())
    with pytest.raises(ValueError, match="defect_files and mentions_any"):
        nak_case(mentions_any=())


def test_an_ack_case_has_no_defect_to_locate() -> None:
    with pytest.raises(ValueError, match="no defect to locate"):
        ack_case(defect_files=("src/sampler.py",))


# -------------------------------------------------------------------- one review


def test_a_defect_caught_in_the_right_place_for_the_right_reason_passes() -> None:
    score = score_case(nak_case(), verdict(Decision.NAK), ran_the_tests(), diff_lines=18)
    assert score.decision_ok
    assert score.located
    assert score.reasoned
    assert score.evidence_ok
    assert score.terminated
    assert score.passed
    assert score.diff_lines == 18
    assert score.notes == ()


def test_a_defect_waved_through_says_what_was_missed() -> None:
    score = score_case(nak_case(), verdict(Decision.ACK), ran_the_tests())
    assert not score.decision_ok
    assert not score.passed
    assert any("waved through" in note for note in score.notes)
    assert any("bottom of the band" in note for note in score.notes)


def test_a_nak_anchored_to_the_wrong_file_does_not_pass() -> None:
    wrong = verdict(Decision.NAK, files=("src/report.py",))
    score = score_case(nak_case(), wrong, ran_the_tests())
    assert score.decision_ok, "it did say NAK"
    assert not score.located
    assert not score.passed
    assert any("anchored to src/report.py" in note for note in score.notes)


def test_a_nak_for_the_wrong_reason_does_not_pass() -> None:
    vague = verdict(Decision.NAK, summary="I do not like this", message="looks wrong to me")
    score = score_case(nak_case(), vague, ran_the_tests())
    assert score.located
    assert not score.reasoned
    assert not score.passed
    assert any("none of LOWER_BOUND, except" in note for note in score.notes)


def test_a_note_written_as_the_diff_shows_the_path_still_counts() -> None:
    as_diff = verdict(Decision.NAK, files=("b/src/sampler.py",))
    assert score_case(nak_case(), as_diff, ran_the_tests()).located


def test_a_correct_change_refused_is_a_false_positive() -> None:
    score = score_case(ack_case(), verdict(Decision.NAK), ran_the_tests())
    assert not score.decision_ok
    assert any("refused a correct change" in note for note in score.notes)


def test_no_verdict_at_all_is_neither_an_ack_nor_a_nak() -> None:
    cut_off = (
        StepFinished(
            stop_reason=StopReason.BUDGET_TOOL_CALLS,
            tool_calls_used=12,
            tokens_used=9000,
            wall_seconds=400.0,
        ),
    )
    score = score_case(nak_case(), None, cut_off)
    assert score.decided is None
    assert not score.decision_ok
    assert not score.terminated
    assert any("no verdict" in note for note in score.notes)


# ---------------------------------------------------------------- the evidence


def test_a_verdict_claiming_green_without_running_the_tests_is_not_evidence() -> None:
    only_finished = (
        StepFinished(
            stop_reason=StopReason.FINAL_ANSWER,
            tool_calls_used=0,
            tokens_used=500,
            wall_seconds=4.0,
        ),
    )
    score = score_case(ack_case(), verdict(Decision.ACK), only_finished)
    assert score.decision_ok, "the decision matched the human"
    assert not score.evidence_ok
    assert not score.passed, (
        "a reviewer that trusts a claim is the failure it exists to prevent"
    )
    assert any("never ran the tests" in note for note in score.notes)


def test_a_verdict_claiming_green_over_a_red_run_is_not_evidence_either() -> None:
    score = score_case(nak_case(), verdict(Decision.NAK), ran_the_tests(green=False))
    assert not score.evidence_ok
    assert any("did not pass" in note for note in score.notes)


def test_a_nak_that_says_the_tests_failed_needs_no_green_run() -> None:
    honest = verdict(Decision.NAK, tests_passed=False)
    score = score_case(nak_case(), honest, ())
    assert score.evidence_ok, "nothing was claimed, so nothing has to be backed"


# ------------------------------------------------------------------- the suite


def built(*cases: CaseScore) -> object:
    return score_reviews(cases)


def test_precision_and_recall_take_nak_as_the_positive_class() -> None:
    score = score_reviews(
        (
            score_case(nak_case(), verdict(Decision.NAK), ran_the_tests()),
            score_case(nak_case(id="second"), verdict(Decision.ACK), ran_the_tests()),
            score_case(ack_case(), verdict(Decision.NAK), ran_the_tests()),
            score_case(ack_case(id="fourth"), verdict(Decision.ACK), ran_the_tests()),
        ),
    )
    assert score.true_positives == 1
    assert score.false_negatives == 1
    assert score.false_positives == 1
    assert score.reviewer_precision == 0.5
    assert score.reviewer_recall == 0.5
    assert score.decision_ratio == 0.5


def test_a_case_with_no_verdict_is_not_folded_into_recall() -> None:
    score = score_reviews(
        (
            score_case(nak_case(), verdict(Decision.NAK), ran_the_tests()),
            score_case(nak_case(id="absent"), None, ()),
        ),
    )
    assert score.undecided == 1
    assert score.false_negatives == 0, "absent is not the same as waved through"
    assert score.reviewer_recall == 1.0


def test_located_and_reasoned_are_measured_over_the_defects_it_caught() -> None:
    score = score_reviews(
        (
            score_case(nak_case(), verdict(Decision.NAK), ran_the_tests()),
            score_case(
                nak_case(id="misplaced"),
                verdict(Decision.NAK, files=("src/report.py",)),
                ran_the_tests(),
            ),
            score_case(nak_case(id="missed"), verdict(Decision.ACK), ran_the_tests()),
        ),
    )
    assert score.true_positives == 2
    assert score.located_ratio == 0.5, "two caught, one anchored correctly"
    assert score.reasoned_ratio == 1.0


def test_the_worst_the_ladder_climbed_is_reported() -> None:
    climbed = score_case(
        nak_case(),
        verdict(Decision.NAK),
        (
            LoopIntervention(
                level=InterventionLevel.SYNTHETIC_RESULT, pathology="exact_repeat"
            ),
            LoopIntervention(
                level=InterventionLevel.REFLECTION_TURN, pathology="progress_stall"
            ),
            *ran_the_tests(),
        ),
    )
    score = score_reviews((climbed,))
    assert climbed.interventions == 2
    assert score.highest_intervention_level == float(InterventionLevel.REFLECTION_TURN)
    assert score.loop_incidents_per_case == 2.0


def test_which_detector_fired_is_recorded_not_only_how_often() -> None:
    """A count cannot say whether a change to the ladder did anything.

    Three passes after the evidence check landed read `evidence_ratio` 1.0
    with interventions in them, which is consistent with the check firing and
    with its never having fired once. The tally is what tells those apart.
    """
    climbed = score_case(
        nak_case(),
        verdict(Decision.NAK),
        (
            LoopIntervention(
                level=InterventionLevel.SYNTHETIC_RESULT, pathology="unbacked_claim"
            ),
            LoopIntervention(
                level=InterventionLevel.SYNTHETIC_RESULT, pathology="exact_repeat"
            ),
            LoopIntervention(level=InterventionLevel.REFLECTION_TURN, pathology="exact_repeat"),
            *ran_the_tests(),
        ),
    )
    quiet = score_case(ack_case(id="quiet"), verdict(Decision.ACK), ran_the_tests())
    assert climbed.pathologies == {"exact_repeat": 2, "unbacked_claim": 1}
    assert quiet.pathologies == {}
    assert score_reviews((climbed, quiet)).pathologies() == {
        "exact_repeat": 2,
        "unbacked_claim": 1,
    }


def test_the_median_diff_size_describes_the_set() -> None:
    sizes = [10, 20, 30, 40]
    score = score_reviews(
        tuple(
            score_case(
                ack_case(id=f"c{n}"), verdict(Decision.ACK), ran_the_tests(), diff_lines=n
            )
            for n in sizes
        ),
    )
    assert score.median_diff_lines == 25.0


# ------------------------------------------------- the suite on disk, and staging


def test_the_golden_set_loads_and_every_case_has_its_files() -> None:
    cases, metrics = load_l2(ROOT)
    assert len(cases) >= 6, "a confusion matrix needs both kinds in useful numbers"
    assert {case.verdict for case in cases} == {Decision.ACK, Decision.NAK}
    assert "reviewer_precision" in metrics
    assert "reviewer_recall" in metrics
    assert len({case.id for case in cases}) == len(cases), "ids are unique"


def test_staging_a_case_makes_one_commit_the_reviewer_can_read(tmp_path: Path) -> None:
    cases, _ = load_l2(ROOT)
    case = next(one for one in cases if one.verdict is Decision.NAK)
    worktree, diff_lines = stage_case(ROOT, case, tmp_path / "repo")

    assert diff_lines > 0, "a case that changes nothing is not a review"
    assert (Path(worktree.path) / "src" / "sampler.py").is_file()
    assert (Path(worktree.path) / "pyproject.toml").is_file(), "the fixture is under it"


def test_every_case_is_green_which_is_the_suites_premise(tmp_path: Path) -> None:
    """Review happens after the gate, so a case the gate would stop is not a case.

    This is the one invariant the whole layer rests on: `Verdict` refuses an
    ACK over red evidence, so a case whose tests fail makes every honest
    verdict a NAK and the precision number meaningless. Checked by running the
    fixture's own gate - ruff and pytest - over each case, because the repo's
    gate is a different gate and does not answer this question.
    """
    cases, _ = load_l2(ROOT)
    fixture = ROOT / "benchmarks" / "suites" / "l2" / "fixture"
    red: list[str] = []
    for case in [None, *cases]:
        name = case.id if case else "_baseline"
        work = tmp_path / name
        shutil.copytree(fixture, work)
        if case is not None:
            shutil.copytree(
                ROOT / "benchmarks" / "suites" / "l2" / "cases" / case.id,
                work,
                dirs_exist_ok=True,
            )
        for argv in (
            ("ruff", "format", "--check", "."),
            ("ruff", "check", "."),
            ("pytest", "-q"),
        ):
            done = subprocess.run(
                [sys.executable, "-m", *argv],
                cwd=work,
                check=False,
                capture_output=True,
                text=True,
            )
            if done.returncode != 0:
                red.append(f"{name}: {' '.join(argv)}\n{done.stdout[-600:]}")
                break
    assert not red, "every case must pass the fixture's gate:\n" + "\n".join(red)


def test_no_answer_key_token_can_be_quoted_from_the_diff() -> None:
    """`mentions_any` has to need the defect understood, not the diff copied.

    The first version of the key failed this: `LOWER_BOUND`, `except` and
    `ValueError` are on lines their cases change, so a note that quoted the
    diff scored as reasoned and `reasoned_ratio` read 1.0 across every pass
    while distinguishing nothing. The rule is checked here rather than
    remembered, because the next case added to the suite will be written by
    somebody who has not read that paragraph.
    """
    suite = ROOT / "benchmarks" / "suites" / "l2"
    cases, _ = load_l2(ROOT)
    checked = 0
    for case in cases:
        if not case.mentions_any:
            continue
        checked += 1
        both_sides: list[str] = []
        root = suite / "cases" / case.id
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            both_sides.append(path.read_text(encoding="utf-8"))
            original = suite / "fixture" / path.relative_to(root)
            if original.is_file():
                both_sides.append(original.read_text(encoding="utf-8"))
        diff_text = " ".join(both_sides).lower()
        quotable = [token for token in case.mentions_any if token.lower() in diff_text]
        assert not quotable, f"{case.id}: {quotable} can be copied out of the diff"
    assert checked == 4, "every NAK case carries an answer key"


def test_a_case_records_which_tools_it_called() -> None:
    """A count of calls cannot tell a refusal that tried from one that did not.

    A reviewer refused a correct change because "the tests cannot be run", in
    a pass where other cases ran them green. Whether that step ever called
    `run_tests` is the whole question, and `calls: 2` does not answer it.
    """
    scored = score_case(
        nak_case(),
        verdict(Decision.NAK),
        (
            *(
                ToolCalled(
                    call_id=CallId(f"r{number}"),
                    name="read_file",
                    arguments={"path": path},
                    arguments_digest=digest_json({"path": path}),
                )
                for number, path in enumerate(("src/sampler.py", "tests/test_sampler.py"))
            ),
            *ran_the_tests(),
        ),
    )
    assert scored.tools["read_file"] == 2
    assert "run_tests" in scored.tools, "the call that earns an evidence claim"
    assert score_reviews((scored,)).tools()["read_file"] == 2


def test_a_case_says_where_its_clock_went() -> None:
    """§11 asks for harness overhead separately from inference, so both are folded.

    Neither was available until the step runner started timing its own calls:
    `ModelResponse` and `ToolReturned` carry a duration now, and what is left
    of the wall after the two of them is the harness's own cost.
    """
    scored = score_case(
        nak_case(),
        verdict(Decision.NAK),
        (
            ModelResponse(
                model="m",
                prompt_tokens=10,
                completion_tokens=5,
                finish_reason="stop",
                content_digest=Digest("0" * 16),
                duration_seconds=4.0,
            ),
            *ran_the_tests(seconds=1.5),
        ),
    )
    assert scored.inference_seconds == 4.0
    assert scored.tool_seconds == 1.5
    suite = score_reviews((scored,))
    assert suite.inference_seconds == 4.0
    assert suite.tool_seconds == 1.5

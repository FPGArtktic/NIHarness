# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scoring L1 from recorded events.

Everything here runs on synthetic event sequences, which is the point of
scoring by folding the log: the grader is testable without a model, so a
number it produces later is about the model and not about the grader.
"""

import shutil
from pathlib import Path

import pytest

from niharness.domain.bench import (
    L0Probe,
    L1Task,
    is_subsequence,
    score_suite,
    score_task,
    score_throughput,
    throughput,
)
from niharness.domain.events import (
    InterventionLevel,
    LoopIntervention,
    Payload,
    StepFinished,
    StopReason,
    ToolCalled,
    ToolRejected,
    ToolReturned,
)
from niharness.domain.identity import CallId, digest_json

READ = L1Task(
    id="read-one-file",
    prompt="Report the first line of README.md.",
    expect_sequence=("read_file",),
    expect_arguments={"read_file": {"path": "README.md"}},
)
REFUSE = L1Task(
    id="refuse-escape",
    prompt="Read /etc/passwd.",
    expect_rejection="absolute_path",
)
EDIT = L1Task(
    id="edit-then-test",
    prompt="Fix the constant and run the tests.",
    expect_sequence=("replace_in_file", "run_tests"),
)


def called(name: str, **arguments: object) -> ToolCalled:
    return ToolCalled(
        call_id=CallId(f"c-{name}"),
        name=name,
        arguments=dict(arguments),
        arguments_digest=digest_json(dict(arguments)),
    )


def rejected(name: str, violation: str) -> ToolRejected:
    return ToolRejected(name=name, violation=violation, detail="refused")


def returned(name: str, *, ok: bool) -> ToolReturned:
    return ToolReturned(
        call_id=CallId(f"c-{name}"),
        ok=ok,
        result_digest=digest_json({"ok": ok}),
        truncated=False,
        summary=f"{name}: {'ok' if ok else 'red'}",
    )


def finished(reason: StopReason = StopReason.FINAL_ANSWER, calls: int = 1) -> StepFinished:
    return StepFinished(
        stop_reason=reason,
        tool_calls_used=calls,
        tokens_used=100,
        wall_seconds=1.0,
    )


def answered(*events: Payload) -> tuple[Payload, ...]:
    """A run that ended on an answer."""
    return (*events, finished(calls=sum(isinstance(e, ToolCalled) for e in events)))


# ------------------------------------------------------------------- sequences


def test_a_task_answered_the_expected_way_passes() -> None:
    score = score_task(READ, answered(called("read_file", path="README.md")))
    assert score.passed
    assert score.notes == ()
    assert (score.calls, score.rejected, score.interventions) == (1, 0, 0)


def test_extra_calls_around_the_expected_ones_are_allowed() -> None:
    """Reading a file before changing it is not a mistake."""
    score = score_task(
        EDIT,
        answered(
            called("read_file", path="src/widget.py"),
            called("replace_in_file", path="src/widget.py", find="3", replace="5"),
            called("run_lint", path="."),
            called("run_tests", selector=""),
        ),
    )
    assert score.sequence_ok
    assert score.passed


def test_the_expected_calls_out_of_order_is_not_the_expected_sequence() -> None:
    score = score_task(
        EDIT,
        answered(called("run_tests", selector=""), called("replace_in_file", path="x")),
    )
    assert not score.sequence_ok
    assert "wanted replace_in_file -> run_tests" in score.notes[0]


def test_a_strict_sequence_admits_nothing_else() -> None:
    task = READ.model_copy(update={"strict_sequence": True})
    extra = answered(called("list_tree", path="."), called("read_file", path="README.md"))
    assert not score_task(task, extra).sequence_ok
    assert score_task(task, answered(called("read_file", path="README.md"))).sequence_ok


def test_a_missing_call_is_reported_with_what_was_seen_instead() -> None:
    score = score_task(READ, answered(called("grep", pattern="first")))
    assert not score.sequence_ok
    assert "saw grep" in score.notes[0]


# ------------------------------------------------------------------- arguments


def test_a_pinned_argument_that_differs_is_named_with_both_values() -> None:
    score = score_task(READ, answered(called("read_file", path="readme.md")))
    assert not score.arguments_ok
    assert any("read_file.path" in note and "readme.md" in note for note in score.notes)


def test_an_argument_that_is_not_pinned_is_not_checked() -> None:
    score = score_task(READ, answered(called("read_file", path="README.md", start_line=4)))
    assert score.arguments_ok


def test_a_tool_that_was_never_called_fails_the_argument_check_too() -> None:
    score = score_task(READ, answered(called("list_tree", path=".")))
    assert not score.arguments_ok
    assert any("never called" in note for note in score.notes)


# ------------------------------------------------------------------- refusals


def test_a_refusal_task_passes_when_the_harness_refused_and_nothing_ran() -> None:
    score = score_task(REFUSE, answered(rejected("read_file", "absolute_path")))
    assert score.passed
    assert score.rejected == 1
    assert score.calls == 0


def test_a_refusal_task_fails_when_the_wrong_rule_fired() -> None:
    score = score_task(REFUSE, answered(rejected("read_file", "path_escape")))
    assert not score.rejection_ok
    assert "wanted absolute_path, saw path_escape" in " ".join(score.notes)


def test_a_refusal_task_tolerates_a_harmless_call_after_the_refusal() -> None:
    """What it measures is that the rule fired, and the refused call never ran.

    The first L1 run failed `refuse-escape` because the model listed the tree
    after being refused `/etc/passwd`. Nothing forbidden happened - a refused
    call does not execute, and path confinement leaves no other route - so
    that was the expectation being wrong rather than the model.
    """
    score = score_task(
        REFUSE,
        answered(rejected("read_file", "absolute_path"), called("list_tree", path=".")),
    )
    assert score.rejection_ok
    assert score.sequence_ok
    assert score.passed


def test_a_task_expecting_no_calls_and_nothing_else_still_demands_silence() -> None:
    """Without a rejection or an end state, an empty sequence means exactly that."""
    silent = L1Task(id="say-nothing", prompt="Answer from what you know.", expect_sequence=())
    assert score_task(silent, answered()).sequence_ok
    assert not score_task(silent, answered(called("list_tree", path="."))).sequence_ok


def test_a_refusal_task_fails_when_nothing_was_refused_at_all() -> None:
    score = score_task(REFUSE, answered())
    assert not score.rejection_ok
    assert "nothing refused" in " ".join(score.notes)


# ------------------------------------------------------------------ terminating


@pytest.mark.parametrize(
    "reason",
    [
        StopReason.BUDGET_TOOL_CALLS,
        StopReason.BUDGET_TOKENS,
        StopReason.BUDGET_WALL_TIME,
        StopReason.DETECTOR_ESCALATION,
        StopReason.ERROR,
    ],
)
def test_a_step_the_harness_ended_did_not_terminate(reason: StopReason) -> None:
    """A budget and a detector are the harness ending what the model did not."""
    score = score_task(READ, (called("read_file", path="README.md"), finished(reason)))
    assert not score.terminated
    assert score.stop_reason is reason
    assert not score.passed
    assert reason.value in " ".join(score.notes)


def test_a_run_with_no_step_finished_is_not_a_termination() -> None:
    score = score_task(READ, (called("read_file", path="README.md"),))
    assert not score.terminated
    assert "no step.finished" in " ".join(score.notes)


# -------------------------------------------------------------------- the suite


def test_the_suite_says_which_detectors_fired() -> None:
    """`loop_incidents_per_task` says how much; this says what for."""
    noisy = score_task(
        EDIT,
        (
            called("replace_in_file", path="x"),
            LoopIntervention(
                level=InterventionLevel.SYNTHETIC_RESULT, pathology="exact_repeat"
            ),
            LoopIntervention(level=InterventionLevel.REFLECTION_TURN, pathology="gate_thrash"),
            finished(StopReason.DETECTOR_ESCALATION, calls=1),
        ),
    )
    quiet = score_task(READ, answered(called("read_file", path="README.md")))
    assert noisy.pathologies == {"exact_repeat": 1, "gate_thrash": 1}
    assert quiet.pathologies == {}
    assert score_suite([noisy, quiet]).pathologies() == {"exact_repeat": 1, "gate_thrash": 1}


def test_the_suite_metrics_are_the_ones_the_suite_declares() -> None:
    good = score_task(READ, answered(called("read_file", path="README.md")))
    bad = score_task(
        EDIT,
        (
            called("replace_in_file", path="x"),
            rejected("shell", "not_allowed"),
            LoopIntervention(
                level=InterventionLevel.SYNTHETIC_RESULT, pathology="exact_repeat"
            ),
            LoopIntervention(level=InterventionLevel.REFLECTION_TURN, pathology="exact_repeat"),
            finished(StopReason.DETECTOR_ESCALATION, calls=1),
        ),
    )
    score = score_suite([good, bad])
    metrics = score.metrics()
    assert metrics["correct_sequence_ratio"] == 0.5
    assert metrics["terminated_ratio"] == 0.5
    assert metrics["passed_ratio"] == 0.5
    assert metrics["valid_calls_ratio"] == pytest.approx(2 / 3, abs=1e-3)
    assert metrics["loop_incidents_per_task"] == 1.0
    assert set(metrics) >= {
        "valid_calls_ratio",
        "correct_sequence_ratio",
        "correct_arguments_ratio",
        "terminated_ratio",
        "loop_incidents_per_task",
    }, "the names the suite's [metrics] section records"


def test_the_suite_says_which_rules_did_the_refusing() -> None:
    """A ratio says how much went wrong; only this says what to change."""
    first = score_task(
        READ,
        answered(
            rejected("read_file", "invalid_arguments"),
            rejected("read_file", "invalid_arguments"),
            called("read_file", path="README.md"),
        ),
    )
    second = score_task(
        REFUSE,
        answered(rejected("read_file", "absolute_path"), rejected("grep", "invalid_arguments")),
    )
    assert first.violations == {"invalid_arguments": 2}
    assert score_suite([first, second]).violations() == {
        "invalid_arguments": 3,
        "absolute_path": 1,
    }, "most frequent first, so the thing worth fixing is at the top"


def test_a_suite_with_nothing_refused_has_nothing_to_report() -> None:
    score = score_suite([score_task(READ, answered(called("read_file", path="README.md")))])
    assert score.violations() == {}


def test_an_empty_suite_scores_zero_rather_than_dividing_by_it() -> None:
    score = score_suite([])
    assert score.metrics()["passed_ratio"] == 0.0
    assert score.loop_incidents_per_task == 0.0


@pytest.mark.parametrize(
    ("wanted", "seen", "expected"),
    [
        (("a", "b"), ("a", "b"), True),
        (("a", "b"), ("a", "x", "b"), True),
        (("a", "b"), ("b", "a"), False),
        (("a", "a"), ("a",), False),
        (("a", "a"), ("a", "a"), True),
        ((), ("a",), True),
    ],
)
def test_a_subsequence_is_order_without_adjacency(
    wanted: tuple[str, ...],
    seen: tuple[str, ...],
    expected: bool,
) -> None:
    assert is_subsequence(wanted, seen) is expected


# ------------------------------------------------------- being called is not enough

EFFECTIVE = L1Task(
    id="edit-then-test",
    prompt="Fix the constant and run the tests.",
    expect_sequence=("replace_in_file", "run_tests"),
    expect_success=("replace_in_file",),
)


def test_a_tool_that_was_called_but_refused_is_not_an_effect() -> None:
    """The hole this closes: a model whose every edit failed used to score.

    `expect_sequence` only asks whether the tool was called. A model whose
    `replace_in_file` was refused and who then ran the tests anyway satisfied
    the sequence while the file was untouched.
    """
    score = score_task(
        EFFECTIVE,
        answered(
            called("replace_in_file", path="src/widget.py"),
            returned("replace_in_file", ok=False),
            called("run_tests", selector=""),
            returned("run_tests", ok=False),
        ),
    )
    assert score.sequence_ok, "the calls were made"
    assert not score.effective, "but nothing happened"
    assert not score.passed
    assert "did not succeed" in " ".join(score.notes)


def test_recovering_from_a_refusal_is_a_skill_not_a_failure() -> None:
    """Only the last call to the tool has to have worked."""
    first = called("replace_in_file", path="src/widget.py", find="x")
    second = called("replace_in_file", path="src/widget.py", find="y")
    score = score_task(
        EFFECTIVE,
        (
            first,
            ToolReturned(
                call_id=first.call_id,
                ok=False,
                result_digest=digest_json({}),
                truncated=False,
                summary="refused",
            ),
            second,
            ToolReturned(
                call_id=second.call_id,
                ok=True,
                result_digest=digest_json({}),
                truncated=False,
                summary="applied",
            ),
            called("run_tests", selector=""),
            returned("run_tests", ok=True),
            finished(calls=3),
        ),
    )
    assert score.effective
    assert score.passed


def test_a_tool_that_had_to_work_and_was_never_called_fails() -> None:
    score = score_task(EFFECTIVE, answered(called("run_tests", selector="")))
    assert not score.effective
    assert "never called" in " ".join(score.notes)


# ------------------------------------------------------------------- restraint


RESTRAINED = L1Task(
    id="do-not-edit-the-test",
    prompt="The code is wrong, not the test.",
    expect_sequence=("replace_in_file",),
    forbid_paths=("tests/",),
)


def test_touching_a_path_the_task_puts_out_of_bounds_fails_it() -> None:
    """The policy sends a wrong test to NOTES.md rather than editing it quietly."""
    score = score_task(
        RESTRAINED,
        answered(called("replace_in_file", path="tests/test_widget.py")),
    )
    assert not score.restrained
    assert "out of bounds" in " ".join(score.notes)
    assert not score.passed


def test_staying_out_of_bounds_passes() -> None:
    score = score_task(
        RESTRAINED,
        answered(
            called("read_file", path="tests/test_widget.py"),
            called("replace_in_file", path="src/widget.py"),
        ),
    )
    assert score.restrained, "reading is not touching; only a named path matters"
    assert score.passed


# ------------------------------------------------------------- the end state

AGREE = L1Task(
    id="two-places-must-agree",
    prompt="Change the limit in both places.",
    expect_content={"src/widget.py": ("RETRY_LIMIT = 5",), "docs/limits.md": ("retries: 5",)},
)


def test_an_end_state_is_scored_without_caring_which_tool_reached_it() -> None:
    """Two edits or one patch across both files are equally correct.

    Scoring the route would fail a model for choosing the other tool, which is
    the mistake the `grep` expectation made on the first run.
    """
    done = {"src/widget.py": "RETRY_LIMIT = 5\n", "docs/limits.md": "- retries: 5\n"}
    by_edits = score_task(AGREE, answered(called("replace_in_file", path="a")), done)
    by_patch = score_task(AGREE, answered(called("apply_patch", diff="...")), done)
    assert by_edits.complete
    assert by_edits.passed
    assert by_patch.complete
    assert by_patch.passed


def test_half_the_work_is_not_complete() -> None:
    """What the first 14-task run caught: one file changed, then committed."""
    score = score_task(
        AGREE,
        answered(
            called("replace_in_file", path="src/widget.py"), called("git_commit", message="x")
        ),
        {"src/widget.py": "RETRY_LIMIT = 5\n", "docs/limits.md": "- retries: 3\n"},
    )
    assert not score.complete
    assert "docs/limits.md does not contain 'retries: 5'" in " ".join(score.notes)
    assert not score.passed


def test_a_file_that_was_never_read_back_is_not_a_pass() -> None:
    """A caller that forgot to read the files must not score one."""
    score = score_task(AGREE, answered(called("replace_in_file", path="a")))
    assert not score.complete
    assert "was not read back" in " ".join(score.notes)


def test_a_task_with_no_end_state_is_unaffected() -> None:
    assert score_task(READ, answered(called("read_file", path="README.md"))).complete


def test_declaring_an_end_state_leaves_the_route_unconstrained() -> None:
    """Not caring which tool also means not caring how many calls."""
    busy = answered(
        called("read_file", path="src/widget.py"),
        called("grep", pattern="retries"),
        called("apply_patch", diff="..."),
    )
    done = {"src/widget.py": "RETRY_LIMIT = 5\n", "docs/limits.md": "- retries: 5\n"}
    assert score_task(AGREE, busy, done).passed


REMOVED = L1Task(
    id="delete-a-passage",
    prompt="Remove the docstring.",
    expect_content={"src/dial.py": ("STEPS = 10",)},
    forbid_content={"src/dial.py": ("How many positions",)},
)


def test_a_removal_is_scored_by_what_is_gone() -> None:
    """Asserting what is present cannot say that something is not.

    The mirror of `expect_content`, and the only way to score a task whose
    point is that something was taken out.
    """
    done = {"src/dial.py": "STEPS = 10\n"}
    left = {"src/dial.py": 'STEPS = 10\n"""How many positions the dial has."""\n'}
    assert score_task(REMOVED, answered(called("replace_in_file", path="a")), done).complete
    score = score_task(REMOVED, answered(called("replace_in_file", path="a")), left)
    assert not score.complete
    assert "still contains" in " ".join(score.notes)


def test_a_removal_that_took_too_much_fails_the_other_half() -> None:
    """Deleting the constant along with its docstring is not the task."""
    score = score_task(
        REMOVED, answered(called("replace_in_file", path="a")), {"src/dial.py": ""}
    )
    assert not score.complete
    assert "does not contain" in " ".join(score.notes)


def test_a_forbidden_fragment_in_a_file_that_is_gone_is_not_a_pass() -> None:
    """A deleted file reads as absent, and absent is not the same as checked."""
    score = score_task(REMOVED, answered(called("replace_in_file", path="a")), {})
    assert not score.complete
    assert "was not read back" in " ".join(score.notes)


def test_a_tool_cache_never_reaches_a_task_fixture(tmp_path: Path) -> None:
    """A fixture's tree hash must not depend on which ruff somebody last ran."""
    from niharness.cli.bench import prepare_fixture

    root = Path(__file__).resolve().parents[2]
    fixture = root / "benchmarks" / "suites" / "l1" / "fixture"
    litter = fixture / ".ruff_cache" / "0.0.0"
    litter.mkdir(parents=True, exist_ok=True)
    (litter / "stale").write_text("whatever ruff left here", encoding="utf-8")
    try:
        worktree = prepare_fixture(root, tmp_path / "repo")
        copied = Path(worktree.path)
        assert (copied / "src" / "widget.py").is_file(), "the fixture itself is copied"
        assert not (copied / ".ruff_cache").exists()
        assert not list(copied.rglob("__pycache__"))
    finally:
        shutil.rmtree(fixture / ".ruff_cache", ignore_errors=True)


# ----------------------------------------------------------------- L0: throughput


def probe(context: int = 8192, generate: int = 64) -> L0Probe:
    return L0Probe(context_tokens=context, generate_tokens=generate)


def test_three_timings_separate_the_prompt_from_the_generation() -> None:
    # The repeated single-token request took 0.01s: per-request overhead plus
    # one token. The cold one took 0.51s, so the prompt cost 0.5s for 8192
    # tokens. The long one took 0.64s, so 63 tokens cost 0.63s.
    sample = throughput(
        probe(),
        prompt_tokens=8192,
        generated=64,
        cold_seconds=0.51,
        warm_seconds=0.01,
        long_seconds=0.64,
    )
    assert sample.generation_tps == 100.0
    assert sample.prompt_tps == pytest.approx(16384.0, rel=0.01)
    assert sample.notes == ()
    assert sample.measured


def test_a_long_request_no_slower_than_the_short_one_measures_nothing() -> None:
    sample = throughput(
        probe(),
        prompt_tokens=8192,
        generated=64,
        cold_seconds=0.9,
        warm_seconds=0.9,
        long_seconds=0.9,
    )
    assert sample.generation_tps == 0.0
    assert sample.prompt_tps == 0.0
    assert not sample.measured
    assert any("nothing to divide" in note for note in sample.notes)


def test_a_prompt_the_cache_already_had_is_reported_as_unmeasured() -> None:
    """The failure the two-request version produced as 68,000 tok/s instead."""
    sample = throughput(
        probe(),
        prompt_tokens=8192,
        generated=64,
        cold_seconds=0.012,
        warm_seconds=0.011,
        long_seconds=0.641,
    )
    assert sample.generation_tps == pytest.approx(100.0, rel=0.02)
    assert sample.prompt_tps == 0.0, "a cached prompt is not a measurement of this host"
    assert any("already cached" in note for note in sample.notes)


def test_the_metrics_are_named_by_the_context_they_were_measured_at() -> None:
    score = score_throughput(
        (
            throughput(
                probe(context=2048),
                prompt_tokens=2048,
                generated=64,
                cold_seconds=0.21,
                warm_seconds=0.01,
                long_seconds=0.64,
            ),
            throughput(
                probe(context=32768),
                prompt_tokens=32768,
                generated=64,
                cold_seconds=1.21,
                warm_seconds=0.01,
                long_seconds=0.64,
            ),
        ),
    )
    assert set(score.metrics()) == {
        "prompt_tps_2k",
        "generation_tps_2k",
        "prompt_tps_32k",
        "generation_tps_32k",
    }
    assert score.metrics()["generation_tps_2k"] == 100.0
    assert score.outcomes() == (), "nothing passes or fails at L0"


def test_the_declared_probes_load() -> None:
    from niharness.cli.bench import load_l0

    root = Path(__file__).resolve().parents[2]
    probes = load_l0(root)
    assert [one.context_tokens for one in probes] == [2048, 8192, 32768, 65536]
    assert [one.id for one in probes] == ["2k-ctx", "8k-ctx", "32k-ctx", "64k-ctx"]

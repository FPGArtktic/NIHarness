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
from pydantic import ValidationError

from niharness.domain.bench import (
    L0Probe,
    L1Task,
    L3Task,
    ReportRow,
    _worth_drawing,
    is_subsequence,
    range_strip_svg,
    report_markdown,
    score_build,
    score_builds,
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
    TaskFinished,
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


# ------------------------------------------------------------- the report


def row(
    commit: str,
    *,
    layer: str = "L2",
    metrics: dict[str, float] | None = None,
    units: dict[str, bool] | None = None,
    dirty: bool = False,
) -> ReportRow:
    return ReportRow(
        layer=layer,
        date="2026-10-24",
        commit=commit,
        served="M",
        quantization="Q",
        dirty=dirty,
        metrics=metrics or {},
        units=units or {},
    )


def test_a_column_is_a_commit_and_a_cell_is_the_spread_across_its_passes() -> None:
    text = report_markdown(
        (
            row("aaaaaaa", metrics={"recall": 0.25}),
            row("bbbbbbb", metrics={"recall": 0.25}),
            row("bbbbbbb", metrics={"recall": 0.75}),
            row("bbbbbbb", metrics={"recall": 0.5}),
        )
    )
    assert "## L2 on M (Q)" in text
    assert "`aaaaaaa` 2026-10-24, 1 pass" in text
    assert "`bbbbbbb` 2026-10-24, 3 passes" in text
    assert "| `recall` | 0.25 | 0.25 - 0.75 |" in text


def test_the_report_says_an_overlap_is_not_a_difference() -> None:
    """The lesson a measurement cost, written where the comparison is read."""
    text = report_markdown((row("aaaaaaa", metrics={"recall": 1.0}),))
    assert "not an error bar" in text
    assert "have not shown a difference" in text


def test_a_metric_one_column_never_recorded_is_a_dash_not_a_zero() -> None:
    text = report_markdown(
        (
            row("aaaaaaa", metrics={"recall": 0.5}),
            row("bbbbbbb", metrics={"recall": 0.5, "pathologies_seen": 2.0}),
        )
    )
    assert "| `pathologies_seen` | - | 2.0 |" in text


def test_a_unit_is_reported_as_passes_out_of_that_column_s_passes() -> None:
    text = report_markdown(
        (
            row("aaaaaaa", units={"case-a": True, "case-b": False}),
            row("aaaaaaa", units={"case-a": False, "case-b": False}),
        )
    )
    assert "| `case-a` | 1/2 |" in text
    assert "| `case-b` | 0/2 |" in text


def test_a_dirty_column_is_marked_and_the_mark_is_explained() -> None:
    text = report_markdown((row("aaaaaaa", metrics={"recall": 1.0}, dirty=True),))
    assert "`aaaaaaa`* 2026-10-24" in text
    assert "a working note, not a result" in text


def test_two_layers_are_two_sections_because_they_are_not_comparable() -> None:
    text = report_markdown(
        (
            row("aaaaaaa", layer="L1", metrics={"passed_ratio": 0.8}),
            row("aaaaaaa", layer="L2", metrics={"passed_ratio": 0.5}),
        )
    )
    assert "## L1 on M (Q)" in text
    assert "## L2 on M (Q)" in text


def test_no_records_says_so_and_says_what_writes_one() -> None:
    assert "nih bench" in report_markdown(())


def test_a_hand_taken_record_is_listed_and_never_folded_into_a_range() -> None:
    """Five hand-taken numbers under one commit are five configurations.

    The first version of this report read them as a five-pass spread and
    printed `prompt_tps 46.28 - 1539.5` - a range across a CPU run and a GPU
    run, presented as the same measurement repeated.
    """
    text = report_markdown(
        (
            ReportRow(
                layer="L0",
                date="2026-10-22",
                commit="aaaaaaa",
                served="M",
                quantization="Q",
                by_hand=True,
                context={"device": "cpu", "measured_by": "llama-bench, by hand"},
                metrics={"prompt_tps": 52.28},
            ),
            ReportRow(
                layer="L0",
                date="2026-10-22",
                commit="aaaaaaa",
                served="M",
                quantization="Q",
                by_hand=True,
                context={"device": "cuda", "measured_by": "llama-bench, by hand"},
                metrics={"prompt_tps": 1539.5},
            ),
        )
    )
    assert "52.28 - 1539.5" not in text, "two configurations are not a spread"
    assert "`prompt_tps` 52.28" in text
    assert "`prompt_tps` 1539.5" in text
    assert "device cpu" in text, "what tells them apart"
    assert "device cuda" in text, "what tells them apart"
    assert text.count("measured_by: llama-bench, by hand") == 1, "said once, not per line"
    assert "| Metric |" not in text, "nothing here is comparable, so no comparison table"


def test_a_ratio_is_drawn_against_zero_to_one_and_a_rate_against_itself() -> None:
    """A fitted axis is the only way a tight spread says anything.

    Three passes spanning 99.22 to 100.02 on a zero-based axis are a
    half-pixel smudge at the ceiling. Fitted, they are a short bar - and the
    labelled ends are what stop that reading as a large difference.
    """
    ratio = range_strip_svg("passed_ratio", (("aaaaaaa", (0.5, 0.75)),))
    assert ">1.0<" in ratio, "a ratio's top is 1.0 whatever was measured"
    assert ">0.0<" in ratio
    rate = range_strip_svg("generation_tps", (("aaaaaaa", (99.22, 100.02)),))
    assert ">100.02<" in rate
    assert ">99.22<" in rate, "both ends, or a fitted axis is a lie"
    assert "99.22 - 100.02" in rate, "the title states the numbers"


def test_a_chart_is_only_drawn_where_something_varies() -> None:
    """One column of one value is a number; a full-height bar invents a comparison."""
    assert _worth_drawing((("aaaaaaa", (1.0,)),)) is False
    assert _worth_drawing((("aaaaaaa", (0.5, 1.0)),)) is True
    assert _worth_drawing((("aaaaaaa", (1.0,)), ("bbbbbbb", (1.0,)))) is True


def test_a_drawing_names_no_colour_of_its_own() -> None:
    """The page has two palettes; a drawing that picks one is wrong in the other."""
    svg = range_strip_svg("passed_ratio", (("aaaaaaa", (0.5, 0.75)),))
    assert "currentColor" in svg
    for hard_coded in ("#", "rgb(", "black", "white"):
        assert hard_coded not in svg, f"{hard_coded} does not follow the theme"


# -------------------------------------------------------------------- L3


def gate(*, passed: bool, call_id: str = "g1") -> tuple[Payload, ...]:
    """A `git_commit` call and its result. That tool is the gate (§4).

    The gate's own `gate.finished` events are appended by the tool server's
    own sink and never reach the run's log, so a fold over them counted zero
    on every task of every pass.
    """
    return (
        ToolCalled(
            call_id=CallId(call_id),
            name="git_commit",
            arguments={"message": "feat: do the thing"},
            arguments_digest=digest_json({"m": call_id}),
        ),
        ToolReturned(
            call_id=CallId(call_id),
            ok=passed,
            result_digest=digest_json({"ok": passed}),
            truncated=False,
            summary="committed" if passed else "gate red",
        ),
    )


def task_finished(
    task_id: str,
    *,
    outcome: str = "completed",
    revisions: int = 0,
    commits: tuple[str, ...] = (),
    reason: str = "",
) -> TaskFinished:
    return TaskFinished(
        task_id=task_id,
        outcome="completed" if outcome == "completed" else "handoff",
        revisions=revisions,
        commits=commits,
        reason=reason,
    )


def build_task(**overrides: object) -> L3Task:
    base: dict[str, object] = {
        "id": "a-task",
        "title": "Add debits alongside credits",
        "intent": "a caller reconciling a statement needs the money out too",
        "acceptance": ("`debits(entries)` returns the negative entries",),
    }
    return L3Task.model_validate(base | overrides)


def test_a_task_without_acceptance_criteria_cannot_be_graded() -> None:
    """Hidden tests check the brief; a brief that says nothing checks nothing."""
    with pytest.raises(ValidationError, match="what done means"):
        L3Task(id="t", title="Do the thing", intent="because")


def test_a_task_passes_when_it_committed_and_the_hidden_tests_agree() -> None:
    events = (
        *gate(passed=False, call_id="g1"),
        *gate(passed=True, call_id="g2"),
        task_finished("a-task", outcome="completed", revisions=1, commits=("abc1234",)),
    )
    scored = score_build(build_task(), events, hidden_tests_passed=True)
    assert scored.passed
    assert scored.gate_attempts == 2
    assert scored.first_attempt_gate is False, "the first offer was red"
    assert scored.revisions == 1
    assert scored.notes == ()


def test_committed_work_the_hidden_tests_refuse_is_a_failure_with_a_reason() -> None:
    """The one measure of success is tests the model never saw.

    A model that writes a test asserting whatever it happened to implement
    passes its own gate and has done nothing, which is why its own evidence
    cannot be the grade.
    """
    scored = score_build(
        build_task(),
        (*gate(passed=True), task_finished("a-task", commits=("abc1234",))),
        hidden_tests_passed=False,
    )
    assert scored.committed is True
    assert scored.passed is False
    assert scored.first_attempt_gate is True, "it was right on the first offer, and still wrong"
    assert "hidden tests refuse" in " ".join(scored.notes)


def test_a_handback_is_counted_as_itself() -> None:
    """The designed ending when the fix loop runs out, not a crash."""
    scored = score_build(
        build_task(),
        (*gate(passed=False), task_finished("a-task", outcome="handoff", reason="fix limit")),
        hidden_tests_passed=False,
    )
    assert scored.handed_back is True
    assert scored.committed is False
    assert "handed back: fix limit" in scored.notes


def test_the_suite_reports_what_landed_and_what_was_right() -> None:
    passed = score_build(
        build_task(id="one"),
        (*gate(passed=True), task_finished("one", commits=("a",))),
        hidden_tests_passed=True,
    )
    wrong = score_build(
        build_task(id="two"),
        (*gate(passed=True), task_finished("two", commits=("b",))),
        hidden_tests_passed=False,
    )
    score = score_builds((passed, wrong))
    assert score.pass_at_1_ratio == 0.5
    assert score.committed_ratio == 1.0, "both landed; one of them was wrong"
    assert score.first_attempt_gate_ratio == 1.0, "the gate is not the measure"
    assert score.outcomes() == (("one", True), ("two", False))
    assert set(score.metrics()) >= {"pass_at_1_ratio", "committed_ratio", "revisions_per_task"}


def test_the_gate_is_counted_from_the_call_the_harness_logs() -> None:
    """A fold over `gate.finished` counted zero on every task of every pass.

    The gate runs inside the tool server, which appends to its own sink, so
    those events never reach the run's log. `git_commit` is the gate, and the
    call and its result are logged - which is where the number has to come
    from.
    """
    scored = score_build(
        build_task(),
        (
            *gate(passed=False, call_id="g1"),
            *gate(passed=True, call_id="g2"),
            task_finished("a-task", commits=("abc1234",)),
        ),
        hidden_tests_passed=True,
    )
    assert scored.gate_attempts == 2
    assert scored.gate_passes == 1
    assert scored.first_attempt_gate is False, "the first offer was red"


def test_work_done_and_never_committed_is_said_in_those_words() -> None:
    """It did not happen, and it is a different failure from being wrong.

    The grade is `HEAD`. This is the only reason the working tree is looked at
    at all: a task that did the work and never offered it to the gate reads
    the same as one that got it wrong, and they are not the same thing.
    """
    scored = score_build(
        build_task(),
        (task_finished("a-task", outcome="handoff", reason="implement failed"),),
        hidden_tests_passed=False,
        uncommitted_would_pass=True,
    )
    assert scored.passed is False
    assert scored.uncommitted_would_pass is True
    assert "never committed" in " ".join(scored.notes)

    landed = score_build(
        build_task(),
        (*gate(passed=True), task_finished("a-task", commits=("abc1234",))),
        hidden_tests_passed=True,
        uncommitted_would_pass=True,
    )
    assert landed.uncommitted_would_pass is False, "it committed; the worktree is not the story"


def test_a_build_records_why_a_call_was_refused() -> None:
    """L1 has had this from the start; L3 went three runs without it.

    A repeated refusal was the dominant failure in every L3 pass -
    `exact_repeat` nine to fourteen times - and the record could say how often
    a detector fired but not what the tool had said, which is the half a
    reader can act on.
    """
    scored = score_build(
        build_task(),
        (
            ToolRejected(
                name="replace_in_file",
                violation="find_not_found",
                detail="the passage does not occur in src/ledger.py; the closest line is 31",
            ),
            ToolRejected(name="replace_in_file", violation="find_not_found", detail="again"),
            task_finished("a-task", outcome="handoff", reason="implement failed"),
        ),
        hidden_tests_passed=False,
    )
    assert scored.rejected == 2
    assert scored.violations == {"find_not_found": 2}
    assert "closest line is 31" in scored.examples["find_not_found"], "the tool's own words"
    suite = score_builds((scored,))
    assert suite.violations() == {"find_not_found": 2}
    assert "find_not_found" in suite.examples()


def test_a_build_records_which_tools_came_back_failed() -> None:
    """A refused call never runs; a failed one ran and did not work.

    `exact_repeat` fires ten to thirteen times a pass in L3 while the refusal
    tally counts one or two, so the repeats are calls that were accepted and
    came back failed - and the record counted calls per tool without their
    outcome, so it could not say which tool.
    """
    scored = score_build(
        build_task(),
        (
            ToolCalled(
                call_id=CallId("e1"),
                name="replace_in_file",
                arguments={"path": "src/ledger.py"},
                arguments_digest=digest_json({"n": 1}),
            ),
            ToolReturned(
                call_id=CallId("e1"),
                ok=False,
                result_digest=digest_json({"n": 1}),
                truncated=False,
                summary="the passage does not occur in src/ledger.py; closest line is 31",
            ),
            ToolCalled(
                call_id=CallId("e2"),
                name="read_file",
                arguments={"path": "src/ledger.py"},
                arguments_digest=digest_json({"n": 2}),
            ),
            ToolReturned(
                call_id=CallId("e2"),
                ok=True,
                result_digest=digest_json({"n": 2}),
                truncated=False,
                summary="read_file src/ledger.py: 40 lines",
            ),
            task_finished("a-task", outcome="handoff", reason="implement failed"),
        ),
        hidden_tests_passed=False,
    )
    assert scored.failures == {"replace_in_file": 1}, "only the call that did not work"
    assert "closest line is 31" in scored.failure_examples["replace_in_file"]
    assert scored.tools == {"replace_in_file": 1, "read_file": 1}, "calls are counted apart"
    suite = score_builds((scored,))
    assert suite.failures() == {"replace_in_file": 1}
    assert "replace_in_file" in suite.failure_examples()

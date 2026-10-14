# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The artifact contracts refuse the documents the gates exist to catch."""

import pytest
from pydantic import ValidationError

from niharness.domain.artifacts import (
    AcceptanceCriterion,
    ContextSummary,
    Decision,
    DodItem,
    FinalAnswer,
    Plan,
    ReviewNote,
    Severity,
    Spec,
    SpecOrigin,
    Task,
    Verdict,
    grounded,
    ungrounded_paths,
)
from niharness.domain.identity import Digest


def criterion(name: str = "c1") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=name,
        statement="nih plan check exits zero on the example plan",
        verification="uv run nih plan check tests/data/PLAN.md",
    )


def spec() -> Spec:
    return Spec(
        title="Example",
        origin=SpecOrigin.HUMAN,
        goal="Make the plan gate usable offline.",
        acceptance_criteria=(criterion(),),
    )


@pytest.mark.parametrize("vague", ["works", "is correct", "TBD", "ok"])
def test_criterion_rejects_untestable_verification(vague: str) -> None:
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="c1", statement="the thing is done", verification=vague)


def test_spec_requires_at_least_one_criterion() -> None:
    with pytest.raises(ValidationError):
        Spec(title="t", origin=SpecOrigin.HUMAN, goal="g", acceptance_criteria=())


def test_spec_rejects_duplicate_criterion_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate acceptance criterion ids"):
        Spec(
            title="t",
            origin=SpecOrigin.HUMAN,
            goal="g",
            acceptance_criteria=(criterion("c1"), criterion("c1")),
        )


def test_spec_digest_is_content_addressed() -> None:
    assert spec().digest() == spec().digest()
    other = spec().model_copy(update={"goal": "something else"})
    assert other.digest() != spec().digest()


def test_plan_rejects_forward_dependency() -> None:
    with pytest.raises(ValidationError, match="do not"):
        Plan(
            spec_digest=Digest("0" * 16),
            tasks=(
                Task(id="a", title="A", intent="do a", depends_on=("b",)),
                Task(id="b", title="B", intent="do b"),
            ),
        )


def test_plan_rejects_duplicate_task_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate task id"):
        Plan(
            spec_digest=Digest("0" * 16),
            tasks=(
                Task(id="a", title="A", intent="do a"),
                Task(id="a", title="A again", intent="do a again"),
            ),
        )


def test_plan_lookup_by_id() -> None:
    plan = Plan(
        spec_digest=Digest("0" * 16),
        tasks=(Task(id="a", title="A", intent="do a"), Task(id="b", title="B", intent="do b")),
    )
    assert plan.task("b").title == "B"
    with pytest.raises(KeyError):
        plan.task("missing")


def test_ack_on_red_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ACK requires"):
        Verdict(
            decision=Decision.ACK,
            summary="looks fine",
            build_passed=True,
            tests_passed=False,
        )


def test_ack_over_a_blocker_note_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ACK contradicts"):
        Verdict(
            decision=Decision.ACK,
            summary="looks fine",
            build_passed=True,
            tests_passed=True,
            notes=(
                ReviewNote(
                    file="src/a.py",
                    line=3,
                    severity=Severity.BLOCKER,
                    message="drops the error",
                ),
            ),
        )


def test_nak_may_carry_blockers_and_red_evidence() -> None:
    verdict = Verdict(
        decision=Decision.NAK,
        summary="build is red",
        build_passed=False,
        tests_passed=False,
        notes=(
            ReviewNote(file="src/a.py", severity=Severity.BLOCKER, message="does not compile"),
        ),
    )
    assert verdict.decision is Decision.NAK


def test_final_answer_completeness() -> None:
    answer = FinalAnswer(
        summary="done",
        dod=(
            DodItem(statement="tests pass", done=True, evidence="pytest: 42 passed"),
            DodItem(statement="docs updated", done=False),
        ),
    )
    assert not answer.complete
    assert answer.model_copy(update={"dod": answer.dod[:1]}).complete


def test_artifacts_are_frozen() -> None:
    task = Task(id="a", title="A", intent="do a")
    with pytest.raises(ValidationError):
        task.title = "B"  # type: ignore[misc]  # frozen models refuse assignment


def test_a_summary_may_not_invent_files_it_never_saw() -> None:
    transcript = "read_file src/niharness/domain/gate.py\napply_patch src/niharness/cli/main.py"
    summary = ContextSummary(
        files_touched=(
            "src/niharness/domain/gate.py",
            "src/niharness/adapters/qdrant/store.py",
        ),
        text="touched the gate and, allegedly, a store that was never mentioned",
    )
    assert ungrounded_paths(summary, transcript) == ("src/niharness/adapters/qdrant/store.py",)
    cleaned = grounded(summary, transcript)
    assert cleaned.files_touched == ("src/niharness/domain/gate.py",)
    assert cleaned.text == summary.text


def test_a_grounded_summary_is_returned_untouched() -> None:
    transcript = "read_file src/a.py"
    summary = ContextSummary(files_touched=("src/a.py",), text="read one file")
    assert grounded(summary, transcript) is summary


def test_quoting_a_path_does_not_make_it_invented() -> None:
    transcript = "the implementer patched src/a.py twice"
    summary = ContextSummary(files_touched=("`src/a.py`",), text="patched it")
    assert ungrounded_paths(summary, transcript) == ()

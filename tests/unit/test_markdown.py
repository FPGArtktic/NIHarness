# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""SPEC.md and PLAN.md round-trip through their artifact block."""

import pytest

from niharness.domain.artifacts import AcceptanceCriterion, Plan, Spec, SpecOrigin, Task
from niharness.domain.identity import Digest
from niharness.domain.markdown import (
    ArtifactNotFoundError,
    extract_json,
    parse_plan,
    parse_spec,
    render_plan,
    render_spec,
)


def spec() -> Spec:
    return Spec(
        title="Offline plan gate",
        origin=SpecOrigin.EXTERNAL_AGENT,
        goal="Validate plans without a model.",
        non_goals=("planning itself",),
        constraints=("air-gapped",),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="c1",
                statement="nih plan check accepts a conforming plan",
                verification="uv run nih plan check PLAN.md exits zero",
            ),
        ),
    )


def plan() -> Plan:
    return Plan(
        spec_digest=spec().digest(),
        tasks=(
            Task(
                id="t1",
                title="Add the parser",
                intent="parse the artifact block",
                satisfies=("c1",),
            ),
            Task(
                id="t2",
                title="Wire the command",
                intent="expose it as nih plan check",
                depends_on=("t1",),
                files=("src/niharness/cli/plan.py",),
                commit_type="feat",
            ),
        ),
        open_questions=("should import normalise ids?",),
    )


def test_spec_round_trip() -> None:
    assert parse_spec(render_spec(spec())) == spec()


def test_plan_round_trip() -> None:
    assert parse_plan(render_plan(plan())) == plan()


def test_rendered_plan_is_readable_for_a_human() -> None:
    text = render_plan(plan())
    assert "## 1. Add the parser" in text
    assert "(after t1)" in text
    assert "## Open questions" in text
    assert "niharness:artifact" in text


def test_bare_json_is_accepted() -> None:
    assert parse_plan(plan().model_dump_json()) == plan()


def test_prose_without_a_block_is_rejected() -> None:
    with pytest.raises(ArtifactNotFoundError):
        parse_plan("# Plan\n\nFirst we do the thing, then the other thing.\n")


def test_the_first_block_wins() -> None:
    document = f"# Plan\n\n```json\n{plan().model_dump_json()}\n```\n\n```json\n{{}}\n```\n"
    assert extract_json(document).startswith("{")
    assert parse_plan(document) == plan()


def test_spec_digest_links_a_plan_to_its_spec() -> None:
    assert parse_plan(render_plan(plan())).spec_digest == spec().digest()
    assert plan().spec_digest != Digest("0" * 16)

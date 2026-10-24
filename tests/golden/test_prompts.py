# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Prompt snapshots.

A prompt change is a commit with a golden update, so the behaviour change is
visible in review and bisectable later. Regenerate with:

    uv run python -m tests.golden.regenerate
"""

from pathlib import Path

import pytest

from niharness.domain.roles import DEFAULT_ROLES, SPEC_GATE_LAYERS, Role
from niharness.prompts import assemble, available_layers, load_layer

pytestmark = pytest.mark.golden

GOLDEN = Path(__file__).parent / "prompts"


@pytest.mark.parametrize("role", list(Role), ids=lambda role: role.value)
def test_assembled_prompt_matches_its_snapshot(role: Role) -> None:
    expected = (GOLDEN / f"{role.value}.md").read_text(encoding="utf-8")
    assert assemble(DEFAULT_ROLES[role].prompt_layers) == expected


def test_the_spec_gates_prompt_matches_its_snapshot() -> None:
    """One step's stack is not a role's, so it needs its own snapshot.

    The spec gate reviews a document nobody has implemented. The reviewer
    layer is about one committed change - re-run the build, anchor each note
    to a line - and would be instructions for the wrong job.
    """
    expected = (GOLDEN / "spec-gate.md").read_text(encoding="utf-8")
    assert assemble(SPEC_GATE_LAYERS) == expected


def test_license_headers_never_reach_the_model() -> None:
    for name in available_layers():
        text = load_layer(name)
        assert "SPDX" not in text
        assert not text.startswith("<!--")


def test_the_policy_layer_states_the_non_negotiables() -> None:
    policy = load_layer("00-policy.md")
    lowered = policy.lower()
    for rule in (
        "no-verify",
        "untested code is not submitted",
        "network",
        "notes.md",
        "conventional commits",
        "one logical change",
    ):
        assert rule in lowered


def test_the_checklist_demands_evidence() -> None:
    checklist = load_layer("20-checklist-dod.md")
    assert "evidence" in checklist
    assert "run_tests" in checklist

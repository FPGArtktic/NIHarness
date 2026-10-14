# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Markdown carriers for artifacts.

`SPEC.md` and `PLAN.md` are written for humans, so they are Markdown; the
harness needs them to be exact, so the authoritative content is a fenced JSON
block inside the document. Prose around the block is for the reader, the block
is for the gate, and the two are rendered from the same model.
"""

from __future__ import annotations

import re

from niharness.domain.artifacts import Plan, Spec

FENCE = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL)
"""The first fenced JSON block of a document is the artifact."""

MARKER = "<!-- niharness:artifact -->"
"""Marks the generated part of a document, so a human knows what not to edit."""


class ArtifactNotFoundError(ValueError):
    """The document carries no artifact block."""


def extract_json(text: str) -> str:
    """Return the artifact JSON carried by `text`.

    Args:
        text: a whole document, or bare JSON.

    Returns:
        The JSON payload as a string.

    Raises:
        ArtifactNotFoundError: when the document has no fenced JSON block.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    match = FENCE.search(text)
    if match is None:
        msg = "no fenced ```json block found; the artifact must be machine-readable"
        raise ArtifactNotFoundError(msg)
    return match.group("body")


def parse_spec(text: str) -> Spec:
    """Parse a `SPEC.md` document into a :class:`Spec`."""
    return Spec.model_validate_json(extract_json(text))


def parse_plan(text: str) -> Plan:
    """Parse a `PLAN.md` document into a :class:`Plan`."""
    return Plan.model_validate_json(extract_json(text))


def render_spec(spec: Spec) -> str:
    """Render a `SPEC.md` document: prose for the human, JSON for the gate."""
    lines = [
        f"# {spec.title}",
        "",
        f"*Origin: {spec.origin.value}.*",
        "",
        "## Goal",
        "",
        spec.goal,
        "",
    ]
    if spec.non_goals:
        lines += ["## Non-goals", "", *[f"- {item}" for item in spec.non_goals], ""]
    if spec.constraints:
        lines += ["## Constraints", "", *[f"- {item}" for item in spec.constraints], ""]
    lines += ["## Acceptance criteria", ""]
    lines += [
        f"- **{criterion.id}**: {criterion.statement}  \n"
        f"  *Verified by:* {criterion.verification}"
        for criterion in spec.acceptance_criteria
    ]
    lines += ["", MARKER, "", "```json", spec.model_dump_json(indent=2), "```", ""]
    return "\n".join(lines)


def render_plan(plan: Plan) -> str:
    """Render a `PLAN.md` document: an ordered task list plus the artifact block."""
    lines = [
        "# Plan",
        "",
        f"*Spec digest: `{plan.spec_digest}`. {len(plan.tasks)} task(s), in execution order.*",
        "",
    ]
    for index, task in enumerate(plan.tasks, start=1):
        depends = f" (after {', '.join(task.depends_on)})" if task.depends_on else ""
        lines += [
            f"## {index}. {task.title}{depends}",
            "",
            f"- id: `{task.id}`",
            f"- commit type: `{task.commit_type}`",
            f"- intent: {task.intent}",
        ]
        if task.satisfies:
            lines.append(f"- satisfies: {', '.join(f'`{item}`' for item in task.satisfies)}")
        if task.files:
            lines.append(f"- files: {', '.join(f'`{item}`' for item in task.files)}")
        lines.append("")
    if plan.open_questions:
        lines += ["## Open questions", "", *[f"- {item}" for item in plan.open_questions], ""]
    lines += [MARKER, "", "```json", plan.model_dump_json(indent=2), "```", ""]
    return "\n".join(lines)

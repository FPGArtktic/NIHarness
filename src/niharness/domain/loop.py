# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-loop pathology detectors and the intervention ladder.

A bounded ReAct loop can still waste its whole budget going in circles. The
detectors in this module are pure functions over the step's event window, so
the same window always yields the same verdict, and the ladder decides how
hard the harness interferes: a synthetic result first, a step failure last.

Thresholds live in `harness.toml` under `[loop]`; the defaults here are the
shipped values.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.events import InterventionLevel
from niharness.domain.identity import Digest


class Pathology(StrEnum):
    """What went wrong with the loop, not with the code."""

    EXACT_REPEAT = "exact_repeat"
    STATE_CYCLE = "state_cycle"
    PROGRESS_STALL = "progress_stall"
    GATE_THRASH = "gate_thrash"
    INVALID_CALLS = "invalid_calls"
    DEGENERATE_OUTPUT = "degenerate_output"


PATHOLOGY_ORDER: tuple[Pathology, ...] = (
    Pathology.INVALID_CALLS,
    Pathology.EXACT_REPEAT,
    Pathology.GATE_THRASH,
    Pathology.STATE_CYCLE,
    Pathology.PROGRESS_STALL,
    Pathology.DEGENERATE_OUTPUT,
)
"""Report order: the most specific signal first, so findings are deterministic."""


class LoopThresholds(BaseModel):
    """The shipped detector thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exact_repeat_window: int = Field(default=5, ge=2)
    exact_repeat_count: int = Field(default=2, ge=2)
    state_cycle_revisits: int = Field(default=1, ge=1)
    progress_stall_calls: int = Field(default=8, ge=2)
    gate_thrash_repeats: int = Field(default=3, ge=2)
    invalid_calls_in_a_row: int = Field(default=3, ge=2)
    degenerate_ngram_size: int = Field(default=8, ge=2)
    degenerate_ngram_repeats: int = Field(default=4, ge=2)


class CallRecord(BaseModel):
    """What the detectors need to know about one executed tool call.

    Every field is derived from events, never from model claims.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    tool: str
    arguments_digest: Digest
    result_digest: Digest | None = None
    tree_digest: Digest | None = Field(
        default=None,
        description="Digest of the worktree after the call; None when the state is unknown.",
    )
    invalid: bool = False
    gate_signature: str = ""


class LoopFinding(BaseModel):
    """One detector firing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pathology: Pathology
    observed: int = Field(ge=0)
    threshold: int = Field(ge=0)
    evidence: str = ""


def _exact_repeat(
    window: Sequence[CallRecord], thresholds: LoopThresholds
) -> LoopFinding | None:
    """Fire when the same tool is called with identical arguments inside a short window."""
    recent = window[-thresholds.exact_repeat_window :]
    counts = Counter((record.tool, record.arguments_digest) for record in recent)
    if not counts:
        return None
    (tool, _), observed = counts.most_common(1)[0]
    if observed < thresholds.exact_repeat_count:
        return None
    return LoopFinding(
        pathology=Pathology.EXACT_REPEAT,
        observed=observed,
        threshold=thresholds.exact_repeat_count,
        evidence=f"{tool} called {observed} times with identical arguments",
    )


def _state_cycle(
    window: Sequence[CallRecord], thresholds: LoopThresholds
) -> LoopFinding | None:
    """Fire when the worktree returns to a state it already had (A then B then A)."""
    seen: dict[Digest, int] = {}
    revisits = 0
    previous: Digest | None = None
    for record in window:
        tree = record.tree_digest
        if tree is None:
            continue
        if tree in seen and previous is not None and previous != tree:
            revisits += 1
        seen.setdefault(tree, record.seq)
        previous = tree
    if revisits < thresholds.state_cycle_revisits:
        return None
    return LoopFinding(
        pathology=Pathology.STATE_CYCLE,
        observed=revisits,
        threshold=thresholds.state_cycle_revisits,
        evidence=f"worktree returned to an earlier state {revisits} time(s)",
    )


def _progress_stall(
    window: Sequence[CallRecord], thresholds: LoopThresholds
) -> LoopFinding | None:
    """Fire on a run of calls that neither changed the worktree nor produced a new result."""
    streak = 0
    seen_results: set[Digest] = set()
    previous_tree: Digest | None = None
    best = 0
    for record in window:
        unchanged = record.tree_digest is not None and record.tree_digest == previous_tree
        repeated = record.result_digest is not None and record.result_digest in seen_results
        streak = streak + 1 if unchanged and repeated else 0
        best = max(best, streak)
        if record.result_digest is not None:
            seen_results.add(record.result_digest)
        if record.tree_digest is not None:
            previous_tree = record.tree_digest
    if best < thresholds.progress_stall_calls:
        return None
    return LoopFinding(
        pathology=Pathology.PROGRESS_STALL,
        observed=best,
        threshold=thresholds.progress_stall_calls,
        evidence=f"{best} consecutive calls changed nothing and returned a known result",
    )


def _gate_thrash(
    window: Sequence[CallRecord], thresholds: LoopThresholds
) -> LoopFinding | None:
    """Fire when the same normalised gate failure keeps coming back."""
    counts = Counter(record.gate_signature for record in window if record.gate_signature)
    if not counts:
        return None
    signature, observed = counts.most_common(1)[0]
    if observed < thresholds.gate_thrash_repeats:
        return None
    return LoopFinding(
        pathology=Pathology.GATE_THRASH,
        observed=observed,
        threshold=thresholds.gate_thrash_repeats,
        evidence=f"gate failure {signature[:12]} repeated {observed} times",
    )


def _invalid_calls(
    window: Sequence[CallRecord], thresholds: LoopThresholds
) -> LoopFinding | None:
    """Fire on consecutive schema or allowlist violations."""
    streak = 0
    for record in reversed(window):
        if not record.invalid:
            break
        streak += 1
    if streak < thresholds.invalid_calls_in_a_row:
        return None
    return LoopFinding(
        pathology=Pathology.INVALID_CALLS,
        observed=streak,
        threshold=thresholds.invalid_calls_in_a_row,
        evidence=f"{streak} rejected calls in a row",
    )


def evaluate(
    window: Sequence[CallRecord],
    thresholds: LoopThresholds | None = None,
) -> tuple[LoopFinding, ...]:
    """Run every call-based detector over `window`.

    Args:
        window: the executed and rejected calls of the current step, in order.
        thresholds: configured thresholds; the shipped defaults when omitted.

    Returns:
        The findings, ordered by :data:`PATHOLOGY_ORDER`. The same window always
        produces the same tuple.
    """
    limits = thresholds or LoopThresholds()
    if not window:
        return ()
    candidates = {
        Pathology.INVALID_CALLS: _invalid_calls(window, limits),
        Pathology.EXACT_REPEAT: _exact_repeat(window, limits),
        Pathology.GATE_THRASH: _gate_thrash(window, limits),
        Pathology.STATE_CYCLE: _state_cycle(window, limits),
        Pathology.PROGRESS_STALL: _progress_stall(window, limits),
    }
    return tuple(
        finding
        for pathology in PATHOLOGY_ORDER
        if (finding := candidates.get(pathology)) is not None
    )


def ngram_repeats(text: str, size: int) -> int:
    """Return how often the most frequent word n-gram occurs in `text`."""
    words = text.split()
    if len(words) < size:
        return 0
    grams = Counter(
        tuple(words[index : index + size]) for index in range(len(words) - size + 1)
    )
    return grams.most_common(1)[0][1]


def degenerate_output(
    text: str, thresholds: LoopThresholds | None = None
) -> LoopFinding | None:
    """Detect a degenerate assistant stream, so generation can be aborted.

    Args:
        text: the assistant text produced so far in this turn.
        thresholds: configured thresholds; the shipped defaults when omitted.

    Returns:
        A finding when an n-gram repeats more often than allowed, else None.
    """
    limits = thresholds or LoopThresholds()
    observed = ngram_repeats(text, limits.degenerate_ngram_size)
    if observed < limits.degenerate_ngram_repeats:
        return None
    return LoopFinding(
        pathology=Pathology.DEGENERATE_OUTPUT,
        observed=observed,
        threshold=limits.degenerate_ngram_repeats,
        evidence=f"a {limits.degenerate_ngram_size}-gram repeated {observed} times",
    )


def next_level(
    applied: Sequence[InterventionLevel],
    findings: Sequence[LoopFinding],
) -> InterventionLevel | None:
    """Return the next rung of the intervention ladder.

    The ladder never goes down and never skips a rung: each detector firing
    moves the step one level further, and level four is step failure.

    Args:
        applied: the levels already applied in this step, in order.
        findings: the findings of the current evaluation.

    Returns:
        The level to apply now, or None when nothing fired.
    """
    if not findings:
        return None
    highest = max(applied, default=None)
    if highest is None:
        return InterventionLevel.SYNTHETIC_RESULT
    if highest is InterventionLevel.STEP_FAILURE:
        return InterventionLevel.STEP_FAILURE
    return InterventionLevel(highest + 1)


def synthetic_result(tool: str, previous_summary: str) -> str:
    """Return the level-one synthetic tool result, produced without any model call.

    Args:
        tool: the tool that was called again with identical arguments.
        previous_summary: the one-line digest of the previous result.

    Returns:
        The text handed back to the model in place of a real execution.
    """
    return (
        f"This exact {tool} call was already executed in this step; it was not run again. "
        f"Digest of the previous result: {previous_summary}. "
        "Choose a different action, or write the current state to NOTES.md and finish."
    )


REFLECTION_INSTRUCTION = (
    "Before any further tool call, answer in plain prose: "
    "(1) what you have tried in this step, "
    "(2) why each attempt failed, "
    "(3) one approach that differs from all of them. "
    "No tool call is accepted in this turn."
)
"""The level-two intervention: a mandatory reflection turn, no tools offered."""

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Context management: the window is a view, compaction is a policy.

The model's message window is derived from the event log and rebuilt every
turn. Compaction rewrites that view only - `events.jsonl` stays complete and
replayable, and anything compacted away is indexed into the vector store so
`rag_query` can recover it.

Token counts always come from the backend tokenizer. This module therefore
never estimates a length: callers pass a `measure` function in, which the
adapter backs with `/tokenize`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.identity import Digest

Measure = Callable[[str], int]
"""A tokenizer: text in, token count out. Supplied by the Tokenizer port."""


class Priority(IntEnum):
    """Window layout. Priority one is never dropped; priority six goes first."""

    PROMPT = 1
    """Prompt layers and the current task from PLAN.md."""

    NOTES = 2
    """The NOTES.md pointer and open reviewer notes."""

    RECENT_TOOL_RESULTS = 3
    """The last N tool results, verbatim."""

    CURRENT_TURNS = 4
    """Assistant and user turns of the current step."""

    OLD_TOOL_RESULTS = 5
    """Older tool results, replaced by one-line digests."""

    OLD_TURNS = 6
    """Older turns, replaced by a ContextSummary."""


NEVER_DROPPED: frozenset[Priority] = frozenset({Priority.PROMPT, Priority.NOTES})
"""Priorities that compaction must preserve verbatim, whatever the fill."""


class ItemKind(StrEnum):
    """What a window item is, which decides how it may be compacted."""

    PROMPT_LAYER = "prompt_layer"
    TASK = "task"
    NOTES = "notes"
    REVIEW_NOTE = "review_note"
    TOOL_RESULT = "tool_result"
    TURN = "turn"
    INSTRUCTION = "instruction"
    SUMMARY = "summary"


TURN_KINDS: frozenset[ItemKind] = frozenset({ItemKind.TURN, ItemKind.INSTRUCTION})
"""Kinds that a ContextSummary may replace: the conversation, not the artefacts."""

LATEST_ONLY_TOOLS: frozenset[str] = frozenset(
    {"run_build", "run_tests", "run_lint", "list_tree", "git_commit"},
)
"""Tools whose older results are superseded: only the latest outcome matters."""


class Action(StrEnum):
    """What compaction did to an item."""

    KEEP = "keep"
    DEDUPE = "dedupe"
    SUPERSEDE = "supersede"
    DIGEST = "digest"
    SUMMARIZE = "summarize"
    DROP = "drop"


class ContextItem(BaseModel):
    """One addressable piece of the window, with its measured token count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    priority: Priority
    kind: ItemKind
    content: str
    tokens: int = Field(ge=0, description="Measured by the backend tokenizer, never estimated.")
    digest: Digest
    origin_seq: int = Field(
        ge=0, description="Event sequence this item came from; 0 for prompts."
    )
    tool: str | None = None
    target: str = Field(default="", description="Path or selector the tool acted on.")
    lines: int | None = Field(default=None, ge=0)

    @property
    def compactable(self) -> bool:
        """True when policy may rewrite or drop this item."""
        return self.priority not in NEVER_DROPPED


class CompactionPolicy(BaseModel):
    """Configured compaction behaviour, from `[compact]` in harness.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_window: int = Field(default=65_536, ge=1_024)
    threshold: float = Field(default=0.75, gt=0.0, le=1.0)
    keep_last_tool_results: int = Field(default=6, ge=0)
    reserve_response_tokens: int = Field(default=2_048, ge=0)
    summary_token_budget: int = Field(default=1_500, ge=0)

    @property
    def trigger_tokens(self) -> int:
        """Fill at which autocompaction runs."""
        return int(self.context_window * self.threshold)

    @property
    def hard_limit(self) -> int:
        """Tokens the window may never exceed, leaving room for the response."""
        return self.context_window - self.reserve_response_tokens


class ItemDecision(BaseModel):
    """What happened to one item, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    action: Action
    reason: str
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)


class CompactionPlan(BaseModel):
    """The rewritten window plus an audit trail of every decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ContextItem, ...]
    decisions: tuple[ItemDecision, ...]
    before_tokens: int = Field(ge=0)
    after_tokens: int = Field(ge=0)
    needs_summary: bool = False
    summarized_ids: tuple[str, ...] = ()
    overflow: bool = Field(
        default=False,
        description="True when even the undroppable items exceed the hard limit.",
    )

    @property
    def deterministic_only(self) -> bool:
        """True when no model call is needed to finish this compaction."""
        return not self.needs_summary


def total_tokens(items: Iterable[ContextItem]) -> int:
    """Return the measured token total of `items`."""
    return sum(item.tokens for item in items)


def should_compact(items: Iterable[ContextItem], policy: CompactionPolicy) -> bool:
    """True when the window has reached the configured trigger fill."""
    return total_tokens(items) >= policy.trigger_tokens


def fill_ratio(items: Iterable[ContextItem], policy: CompactionPolicy) -> float:
    """Return window fill as a fraction of the context window, clamped to one."""
    return min(1.0, total_tokens(items) / policy.context_window)


def digest_line(item: ContextItem) -> str:
    """Return the one-line stand-in for a compacted tool result.

    The shape is `read_file src/net.c: 412 lines, sha 3f9a...`, so the model can
    tell what it already saw and ask for it again with `rag_query` if needed.
    """
    tool = item.tool or item.kind.value
    where = f" {item.target}" if item.target else ""
    size = f"{item.lines} lines, " if item.lines is not None else ""
    return f"{tool}{where}: {size}sha {item.digest}"


def _rewrite(item: ContextItem, content: str, measure: Measure) -> ContextItem:
    """Return `item` with new content and a freshly measured token count."""
    return item.model_copy(update={"content": content, "tokens": measure(content)})


def plan_compaction(  # noqa: C901 - one readable pass per layout rule
    items: Sequence[ContextItem],
    policy: CompactionPolicy,
    measure: Measure,
) -> CompactionPlan:
    """Compact the window down to the configured target.

    The passes run in the order of the window layout, cheapest first, and stop
    as soon as the window fits:

    1. drop duplicate tool results that returned identical content,
    2. drop tool results superseded by a later run of the same tool,
    3. replace tool results older than the keep window with digest lines,
    4. mark older turns for a ContextSummary (the only pass needing a model),
    5. drop remaining low-priority items, oldest first.

    Priority one and two are never touched, in any pass.

    Args:
        items: the current window, oldest first, with measured token counts.
        policy: the configured limits.
        measure: the backend tokenizer, used for every rewritten item.

    Returns:
        A plan holding the new window, the decisions and the token totals.
    """
    before = total_tokens(items)
    target = min(policy.trigger_tokens, policy.hard_limit)
    decisions: list[ItemDecision] = []
    kept: dict[str, ContextItem] = {item.id: item for item in items}
    order = [item.id for item in items]

    def current() -> list[ContextItem]:
        return [kept[item_id] for item_id in order if item_id in kept]

    def fits() -> bool:
        return total_tokens(current()) <= target

    def remove(item: ContextItem, action: Action, reason: str) -> None:
        del kept[item.id]
        decisions.append(
            ItemDecision(
                item_id=item.id,
                action=action,
                reason=reason,
                tokens_before=item.tokens,
                tokens_after=0,
            ),
        )

    # Pass 1: identical content from the same tool and target.
    seen: dict[tuple[str, str, str], str] = {}
    for item in items:
        if item.kind is not ItemKind.TOOL_RESULT or not item.compactable:
            continue
        key = (item.tool or "", item.target, str(item.digest))
        earlier = seen.get(key)
        if earlier is not None and earlier in kept:
            remove(kept[earlier], Action.DEDUPE, "identical result returned again later")
        seen[key] = item.id

    # Pass 2: only the latest build, test, lint or listing matters.
    latest: dict[tuple[str, str], str] = {}
    for item in items:
        if item.kind is not ItemKind.TOOL_RESULT or item.tool not in LATEST_ONLY_TOOLS:
            continue
        latest_key = (item.tool or "", item.target)
        previous = latest.get(latest_key)
        if previous is not None and previous in kept:
            remove(kept[previous], Action.SUPERSEDE, f"superseded by a later {item.tool}")
        latest[latest_key] = item.id

    def finish(
        *, needs_summary: bool = False, summarized: tuple[str, ...] = ()
    ) -> CompactionPlan:
        """Close the plan, recording every untouched item as kept."""
        window = current()
        decided = {decision.item_id for decision in decisions}
        for entry in window:
            if entry.id in decided:
                continue
            decisions.append(
                ItemDecision(
                    item_id=entry.id,
                    action=Action.KEEP,
                    reason="kept",
                    tokens_before=entry.tokens,
                    tokens_after=entry.tokens,
                ),
            )
        after = total_tokens(window)
        return CompactionPlan(
            items=tuple(window),
            decisions=tuple(decisions),
            before_tokens=before,
            after_tokens=after,
            needs_summary=needs_summary,
            summarized_ids=summarized,
            overflow=after > policy.hard_limit,
        )

    if fits():
        return finish()

    # Pass 3: digest tool results outside the keep window.
    results = [
        item for item in current() if item.kind is ItemKind.TOOL_RESULT and item.compactable
    ]
    older = results[: max(0, len(results) - policy.keep_last_tool_results)]
    for item in older:
        line = digest_line(item)
        replacement = _rewrite(
            item.model_copy(update={"priority": Priority.OLD_TOOL_RESULTS}),
            line,
            measure,
        )
        if replacement.tokens >= item.tokens:
            continue
        kept[item.id] = replacement
        decisions.append(
            ItemDecision(
                item_id=item.id,
                action=Action.DIGEST,
                reason="older than the verbatim keep window",
                tokens_before=item.tokens,
                tokens_after=replacement.tokens,
            ),
        )
        if fits():
            break

    needs_summary = False
    summarized: list[str] = []
    if not fits():
        # Pass 4: older turns become one ContextSummary, produced by the utility model.
        turns = [item for item in current() if item.kind in TURN_KINDS and item.compactable]
        stale = [item for item in turns if item.priority >= Priority.OLD_TURNS] or turns[:-2]
        for item in stale:
            remove(item, Action.SUMMARIZE, "folded into a ContextSummary")
            summarized.append(item.id)
            needs_summary = True
            if total_tokens(current()) + policy.summary_token_budget <= target:
                break

    if not fits():
        # Pass 5: last resort, drop the lowest priority and oldest first.
        droppable = sorted(
            (item for item in current() if item.compactable),
            key=lambda item: (-int(item.priority), item.origin_seq),
        )
        for item in droppable:
            remove(item, Action.DROP, "window still above the hard limit")
            if fits():
                break

    return finish(needs_summary=needs_summary, summarized=tuple(summarized))

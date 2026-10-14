# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compaction is a view policy with invariants, not a best effort."""

from hypothesis import given, settings
from hypothesis import strategies as st

from niharness.domain.context import (
    NEVER_DROPPED,
    Action,
    CompactionPolicy,
    ContextItem,
    ItemKind,
    Priority,
    digest_line,
    fill_ratio,
    plan_compaction,
    should_compact,
    total_tokens,
)
from niharness.domain.identity import digest_text

POLICY = CompactionPolicy(context_window=4_096, threshold=0.5, keep_last_tool_results=2)


def measure(text: str) -> int:
    """A deterministic stand-in for the backend tokenizer."""
    return max(1, len(text) // 4)


def item(
    name: str,
    priority: Priority,
    kind: ItemKind,
    tokens: int,
    *,
    tool: str | None = None,
    target: str = "",
    content: str | None = None,
    seq: int = 1,
    lines: int | None = None,
) -> ContextItem:
    body = content if content is not None else "x" * (tokens * 4)
    return ContextItem(
        id=name,
        priority=priority,
        kind=kind,
        content=body,
        tokens=tokens,
        digest=digest_text(body),
        origin_seq=seq,
        tool=tool,
        target=target,
        lines=lines,
    )


def prompt(tokens: int = 100) -> ContextItem:
    return item("prompt", Priority.PROMPT, ItemKind.PROMPT_LAYER, tokens, seq=0)


def notes(tokens: int = 50) -> ContextItem:
    return item("notes", Priority.NOTES, ItemKind.NOTES, tokens, seq=0)


def tool_result(
    name: str,
    tokens: int,
    *,
    target: str,
    tool: str = "read_file",
    seq: int = 1,
) -> ContextItem:
    return item(
        name,
        Priority.RECENT_TOOL_RESULTS,
        ItemKind.TOOL_RESULT,
        tokens,
        tool=tool,
        target=target,
        seq=seq,
        lines=tokens,
    )


def test_threshold_decides_when_to_compact() -> None:
    small = [prompt(10)]
    assert not should_compact(small, POLICY)
    assert should_compact([prompt(POLICY.trigger_tokens)], POLICY)
    assert fill_ratio([prompt(2_048)], POLICY) == 0.5


def test_identical_results_are_deduplicated() -> None:
    window = [
        prompt(),
        tool_result("r1", 600, target="src/a.py", seq=1),
        tool_result("r2", 600, target="src/a.py", seq=2),
        tool_result("r3", 600, target="src/a.py", seq=3),
    ]
    plan = plan_compaction(window, POLICY, measure)
    actions = {decision.item_id: decision.action for decision in plan.decisions}
    assert actions["r1"] is Action.DEDUPE
    assert actions["r2"] is Action.DEDUPE
    assert actions["r3"] is Action.KEEP


def test_only_the_latest_build_result_is_kept() -> None:
    window = [
        prompt(),
        item(
            "b1",
            Priority.RECENT_TOOL_RESULTS,
            ItemKind.TOOL_RESULT,
            700,
            tool="run_build",
            content="first build output",
            seq=1,
        ),
        item(
            "b2",
            Priority.RECENT_TOOL_RESULTS,
            ItemKind.TOOL_RESULT,
            700,
            tool="run_build",
            content="second build output",
            seq=2,
        ),
    ]
    plan = plan_compaction(window, POLICY, measure)
    kept = {entry.id for entry in plan.items}
    assert kept == {"prompt", "b2"}
    assert any(
        decision.action is Action.SUPERSEDE and decision.item_id == "b1"
        for decision in plan.decisions
    )


def test_old_results_become_digest_lines() -> None:
    window = [prompt()] + [
        tool_result(f"r{index}", 700, target=f"src/{index}.py", seq=index)
        for index in range(1, 6)
    ]
    plan = plan_compaction(window, POLICY, measure)
    digested = [
        decision.item_id for decision in plan.decisions if decision.action is Action.DIGEST
    ]
    assert digested
    for entry in plan.items:
        if entry.id in digested:
            assert entry.content == digest_line(window[int(entry.id[1:])])
            assert entry.priority is Priority.OLD_TOOL_RESULTS


def test_digest_line_shape() -> None:
    entry = tool_result("r1", 412, target="src/net.c")
    line = digest_line(entry)
    assert line.startswith("read_file src/net.c: 412 lines, sha ")


def test_older_turns_are_folded_into_a_summary() -> None:
    window = [
        prompt(),
        *[
            item(f"t{index}", Priority.OLD_TURNS, ItemKind.TURN, 500, seq=index)
            for index in range(1, 6)
        ],
    ]
    plan = plan_compaction(window, POLICY, measure)
    assert plan.needs_summary
    assert plan.summarized_ids
    assert not plan.deterministic_only


def test_priority_one_and_two_survive_everything() -> None:
    window = [
        prompt(3_000),
        notes(500),
        *[
            item(f"t{index}", Priority.OLD_TURNS, ItemKind.TURN, 400, seq=index)
            for index in range(1, 10)
        ],
    ]
    plan = plan_compaction(window, POLICY, measure)
    survivors = {entry.id: entry for entry in plan.items}
    assert "prompt" in survivors
    assert "notes" in survivors
    assert survivors["prompt"].tokens == 3_000


def test_overflow_is_reported_when_the_undroppable_part_is_too_big() -> None:
    plan = plan_compaction([prompt(10_000)], POLICY, measure)
    assert plan.overflow
    assert plan.items[0].id == "prompt"


def test_a_window_that_fits_is_left_alone() -> None:
    window = [prompt(100), notes(20)]
    plan = plan_compaction(window, POLICY, measure)
    assert plan.before_tokens == plan.after_tokens == 120
    assert all(decision.action is Action.KEEP for decision in plan.decisions)


items_strategy = st.lists(
    st.builds(
        item,
        st.text(alphabet="abcdefghij", min_size=1, max_size=4).map(lambda name: f"i{name}"),
        st.sampled_from(list(Priority)),
        st.sampled_from(list(ItemKind)),
        st.integers(min_value=1, max_value=900),
    ),
    max_size=14,
    unique_by=lambda entry: entry.id,
)


@given(items_strategy)
@settings(max_examples=60, deadline=None)
def test_compaction_never_grows_the_window(window: list[ContextItem]) -> None:
    plan = plan_compaction(window, POLICY, measure)
    assert plan.after_tokens <= plan.before_tokens
    assert plan.before_tokens == total_tokens(window)


@given(items_strategy)
@settings(max_examples=60, deadline=None)
def test_undroppable_items_are_always_preserved(window: list[ContextItem]) -> None:
    plan = plan_compaction(window, POLICY, measure)
    kept = {entry.id: entry for entry in plan.items}
    for entry in window:
        if entry.priority in NEVER_DROPPED:
            assert kept[entry.id].content == entry.content


@given(items_strategy)
@settings(max_examples=60, deadline=None)
def test_window_fits_unless_overflow_is_declared(window: list[ContextItem]) -> None:
    plan = plan_compaction(window, POLICY, measure)
    target = min(POLICY.trigger_tokens, POLICY.hard_limit)
    assert plan.after_tokens <= target or plan.overflow or plan.needs_summary


@given(items_strategy)
@settings(max_examples=60, deadline=None)
def test_compaction_is_deterministic(window: list[ContextItem]) -> None:
    assert plan_compaction(window, POLICY, measure) == plan_compaction(window, POLICY, measure)

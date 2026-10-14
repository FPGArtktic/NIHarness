# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Window assembly: ordering, system merging, and no reasoning in the history."""

from niharness.domain.context import ContextItem, ItemKind, Priority
from niharness.domain.identity import digest_text
from niharness.domain.messages import (
    ChatResponse,
    Message,
    MessageRole,
    Usage,
    build_messages,
    window_items_by_priority,
)


def item(
    name: str, priority: Priority, kind: ItemKind, content: str, seq: int = 1
) -> ContextItem:
    return ContextItem(
        id=name,
        priority=priority,
        kind=kind,
        content=content,
        tokens=len(content),
        digest=digest_text(content),
        origin_seq=seq,
        tool="read_file" if kind is ItemKind.TOOL_RESULT else None,
    )


def test_system_layers_are_merged_and_lead() -> None:
    messages = build_messages(
        [
            item("turn", Priority.CURRENT_TURNS, ItemKind.TURN, "assistant said", seq=5),
            item("policy", Priority.PROMPT, ItemKind.PROMPT_LAYER, "policy", seq=0),
            item("role", Priority.PROMPT, ItemKind.PROMPT_LAYER, "role", seq=1),
        ],
    )
    assert messages[0].role is MessageRole.SYSTEM
    assert messages[0].content == "policy\n\nrole"
    assert len([m for m in messages if m.role is MessageRole.SYSTEM]) == 1


def test_priority_then_event_order_decides_position() -> None:
    messages = build_messages(
        [
            item("r2", Priority.RECENT_TOOL_RESULTS, ItemKind.TOOL_RESULT, "second", seq=9),
            item("r1", Priority.RECENT_TOOL_RESULTS, ItemKind.TOOL_RESULT, "first", seq=4),
            item("task", Priority.PROMPT, ItemKind.TASK, "the task", seq=0),
        ],
    )
    assert [message.content for message in messages] == ["the task", "first", "second"]


def test_tool_results_carry_their_call_id() -> None:
    messages = build_messages(
        [item("call-7", Priority.RECENT_TOOL_RESULTS, ItemKind.TOOL_RESULT, "output")],
    )
    assert messages[0].role is MessageRole.TOOL
    assert messages[0].tool_call_id == "call-7"
    assert messages[0].name == "read_file"


def test_a_message_has_no_place_for_reasoning() -> None:
    assert "reasoning_content" not in Message.model_fields
    response = ChatResponse(
        content="answer",
        reasoning_content="private deliberation",
        usage=Usage(prompt_tokens=10, completion_tokens=2),
    )
    rebuilt = Message(role=MessageRole.ASSISTANT, content=response.content)
    assert "deliberation" not in rebuilt.model_dump_json()


def test_token_totals_are_reported_per_priority() -> None:
    totals = window_items_by_priority(
        [
            item("a", Priority.PROMPT, ItemKind.PROMPT_LAYER, "x" * 10),
            item("b", Priority.OLD_TURNS, ItemKind.TURN, "y" * 4),
        ],
    )
    assert totals[Priority.PROMPT] == 10
    assert totals[Priority.OLD_TURNS] == 4
    assert totals[Priority.NOTES] == 0


def test_usage_totals() -> None:
    assert Usage(prompt_tokens=7, completion_tokens=3).total == 10

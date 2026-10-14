# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Message types.

Assembling a window into an ordered message list lives in the application
layer and is tested in `test_window.py`; what is tested here is what the types
themselves guarantee.
"""

from niharness.domain.context import ContextItem, ItemKind, Priority
from niharness.domain.identity import CallId, digest_text
from niharness.domain.messages import (
    ChatResponse,
    Message,
    MessageRole,
    ToolCallRequest,
    Usage,
    strip_reasoning,
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
    )


def test_a_message_has_no_place_for_reasoning() -> None:
    assert "reasoning_content" not in Message.model_fields
    response = ChatResponse(
        content="answer",
        reasoning_content="private deliberation",
        usage=Usage(prompt_tokens=10, completion_tokens=2),
    )
    rebuilt = Message(role=MessageRole.ASSISTANT, content=response.content)
    assert "deliberation" not in rebuilt.model_dump_json()


def test_stripping_reasoning_from_a_foreign_history_changes_nothing_visible() -> None:
    history = (
        Message(role=MessageRole.SYSTEM, content="policy"),
        Message(role=MessageRole.ASSISTANT, content="answer"),
    )
    assert strip_reasoning(history) == history


def test_an_assistant_message_carries_the_calls_it_proposed() -> None:
    message = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(
            ToolCallRequest(call_id=CallId("c1"), name="read_file", arguments_json="{}"),
        ),
    )
    assert message.tool_calls[0].call_id == "c1"


def test_a_tool_message_is_paired_with_its_call() -> None:
    message = Message(
        role=MessageRole.TOOL,
        content="file body",
        tool_call_id=CallId("c1"),
        name="read_file",
    )
    assert (message.tool_call_id, message.name) == ("c1", "read_file")


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

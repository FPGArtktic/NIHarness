# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chat messages and the request the harness sends to an inference backend.

These are the harness's own types, not a backend's wire format. The adapter
translates them into the OpenAI-compatible payload, which is the only protocol
the harness speaks.

Reasoning is logged, never fed back: :func:`build_messages` has no way to put
`reasoning_content` into a history, which is deliberate.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.context import ContextItem, ItemKind, Priority
from niharness.domain.identity import CallId


class MessageRole(StrEnum):
    """Who authored a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCallRequest(BaseModel):
    """A tool call as it appears in an assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: CallId
    name: str
    arguments_json: str


class Message(BaseModel):
    """One message of the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MessageRole
    content: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: CallId | None = None
    name: str = ""


class ToolSchema(BaseModel):
    """A tool as advertised to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    parameters: dict[str, object]


class ToolChoice(StrEnum):
    """Whether the model may call tools in this turn."""

    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


class ResponseFormat(BaseModel):
    """A structured-output constraint: a JSON Schema and, for llama-server, a grammar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    json_schema: dict[str, object]
    grammar: str = Field(default="", description="GBNF; used when the backend supports it.")


class ChatRequest(BaseModel):
    """One inference request. Determinism knobs are explicit, never defaulted by the backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolSchema, ...] = ()
    tool_choice: ToolChoice = ToolChoice.AUTO
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    max_tokens: int = Field(default=4_096, ge=1)
    seed: int | None = None
    response_format: ResponseFormat | None = None
    stop: tuple[str, ...] = ()


class Usage(BaseModel):
    """Token accounting as reported by the backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)

    @property
    def total(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens


class ChatResponse(BaseModel):
    """One inference response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str = ""
    reasoning_content: str = Field(
        default="",
        description="Logged to the event stream and shown in the TUI; never fed back.",
    )
    tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str = "stop"
    usage: Usage


_ROLE_BY_KIND: dict[ItemKind, MessageRole] = {
    ItemKind.PROMPT_LAYER: MessageRole.SYSTEM,
    ItemKind.TASK: MessageRole.USER,
    ItemKind.NOTES: MessageRole.USER,
    ItemKind.REVIEW_NOTE: MessageRole.USER,
    ItemKind.SUMMARY: MessageRole.USER,
    ItemKind.TOOL_RESULT: MessageRole.TOOL,
    ItemKind.TURN: MessageRole.ASSISTANT,
}


def build_messages(items: Iterable[ContextItem]) -> tuple[Message, ...]:
    """Assemble a compacted window into a message list.

    Items are emitted in priority order and, within a priority, in event order,
    so the system prompt leads and the freshest tool results sit closest to the
    model's turn.

    Args:
        items: the window produced by :func:`niharness.domain.context.plan_compaction`.

    Returns:
        The messages to send. System layers are merged into one message,
        because backends treat several system messages inconsistently.
    """
    ordered = sorted(items, key=lambda item: (int(item.priority), item.origin_seq, item.id))
    system_parts: list[str] = []
    rest: list[Message] = []
    for item in ordered:
        role = _ROLE_BY_KIND[item.kind]
        if role is MessageRole.SYSTEM:
            system_parts.append(item.content)
            continue
        rest.append(
            Message(
                role=role,
                content=item.content,
                tool_call_id=CallId(item.id) if role is MessageRole.TOOL else None,
                name=item.tool or "",
            ),
        )
    messages: list[Message] = []
    if system_parts:
        messages.append(Message(role=MessageRole.SYSTEM, content="\n\n".join(system_parts)))
    messages.extend(rest)
    return tuple(messages)


def strip_reasoning(history: Sequence[Message]) -> tuple[Message, ...]:
    """Return `history` with assistant reasoning removed.

    The harness never stores reasoning in a message, so this is a guard for
    histories assembled elsewhere, and a place for the rule to be tested.
    """
    return tuple(
        message if message.role is not MessageRole.ASSISTANT else message.model_copy()
        for message in history
    )


def window_items_by_priority(items: Iterable[ContextItem]) -> dict[Priority, int]:
    """Return the token total per priority, for the TUI context gauge."""
    totals: dict[Priority, int] = dict.fromkeys(Priority, 0)
    for item in items:
        totals[item.priority] += item.tokens
    return totals

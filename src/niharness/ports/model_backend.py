# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The inference port.

The harness speaks one protocol: OpenAI-compatible HTTP over localhost. There
are no backend-specific branches; differences between `llama-server` and
`bitnet.cpp` live in profile TOML, not in code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.messages import ChatRequest, ChatResponse


class BackendInfo(BaseModel):
    """What a backend reports about itself at startup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    context_window: int = Field(ge=1_024)
    supports_grammar: bool = Field(
        default=False,
        description="True when the backend accepts GBNF, for stricter structured output.",
    )
    supports_reasoning: bool = Field(
        default=False,
        description="True when responses may carry reasoning_content.",
    )
    base_url: str = Field(description="Must be a localhost URL; the harness is air-gapped.")


@runtime_checkable
class ModelBackend(Protocol):
    """A chat-completions endpoint the harness can drive."""

    async def info(self) -> BackendInfo:
        """Return the backend's model, context window and capabilities."""
        ...

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run one completion.

        Implementations must surface `reasoning_content` separately from
        `content`, because reasoning is logged and never fed back.
        """
        ...

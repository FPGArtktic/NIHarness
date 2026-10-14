# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Producing a ContextSummary with the utility model.

Compaction's deterministic passes cost nothing but a tokenizer call. Only when
they are not enough does a model get involved, and it is the small one: the
ternary utility model on CPU, with no tools, a temperature of zero and a schema
it must satisfy.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from niharness.domain.artifacts import ContextSummary
from niharness.domain.messages import (
    ChatRequest,
    Message,
    MessageRole,
    ResponseFormat,
    ToolChoice,
)
from niharness.domain.roles import DOD_LAYER, POLICY_LAYER, Role, spec_for
from niharness.ports.model_backend import ModelBackend
from niharness.prompts import assemble

_log = structlog.get_logger(__name__)


class Summarizer(Protocol):
    """Turns a transcript into a :class:`ContextSummary`."""

    async def summarize(self, transcript: str) -> ContextSummary:
        """Return a summary of `transcript`."""
        ...


class ModelSummarizer:
    """A :class:`Summarizer` backed by the utility model."""

    def __init__(self, backend: ModelBackend, model: str, *, max_tokens: int = 1_024) -> None:
        """Wire the backend that serves the compactor role.

        Args:
            backend: the utility model's endpoint.
            model: the model name to request.
            max_tokens: cap on the summary itself.
        """
        self._backend = backend
        self._model = model
        self._max_tokens = max_tokens
        layers = tuple(
            layer for layer in spec_for(Role.COMPACTOR).prompt_layers if layer != DOD_LAYER
        )
        self._system = assemble(layers if layers else (POLICY_LAYER,))

    async def summarize(self, transcript: str) -> ContextSummary:
        """Return a summary of `transcript`.

        Raises:
            ValueError: if the model's answer does not validate against the
                ContextSummary schema. The caller decides what to do about it;
                losing the turns silently is not an option.
        """
        request = ChatRequest(
            model=self._model,
            messages=(
                Message(role=MessageRole.SYSTEM, content=self._system),
                Message(role=MessageRole.USER, content=transcript),
            ),
            tool_choice=ToolChoice.NONE,
            temperature=0.0,
            max_tokens=self._max_tokens,
            response_format=ResponseFormat(
                name="ContextSummary",
                json_schema=ContextSummary.model_json_schema(),
            ),
        )
        response = await self._backend.chat(request)
        summary = ContextSummary.model_validate_json(response.content)
        _log.debug(
            "context.summarised",
            decisions=len(summary.decisions),
            failed_attempts=len(summary.failed_attempts),
            tokens=response.usage.completion_tokens,
        )
        return summary

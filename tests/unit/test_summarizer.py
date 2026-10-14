# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The utility model is the smallest model here, so its output is checked."""

import pytest

from niharness.application.summarizer import ModelSummarizer
from niharness.domain.artifacts import ContextSummary
from niharness.domain.messages import ChatRequest, ChatResponse, MessageRole, ToolChoice, Usage
from niharness.ports.model_backend import BackendInfo

TRANSCRIPT = "read_file src/niharness/domain/gate.py\napply_patch src/niharness/cli/main.py"


class FakeBackend:
    """Answers with a prepared summary and records the request."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[ChatRequest] = []

    async def info(self) -> BackendInfo:
        return BackendInfo(
            model="BitNet-b1.58-2B-4T",
            context_window=4_096,
            base_url="http://127.0.0.1:8081",
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            content=self.content,
            finish_reason="stop",
            usage=Usage(prompt_tokens=800, completion_tokens=120),
        )


def summary_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "decisions": ["keep the parser in the domain"],
        "files_touched": ["src/niharness/domain/gate.py"],
        "text": "decided to keep the parser in the domain",
    }
    return ContextSummary.model_validate(payload | overrides).model_dump_json()


async def test_the_summary_is_requested_with_a_schema_and_no_tools() -> None:
    backend = FakeBackend(summary_json())
    await ModelSummarizer(backend, "BitNet-b1.58-2B-4T").summarize(TRANSCRIPT)
    request = backend.requests[0]
    assert request.tool_choice is ToolChoice.NONE
    assert request.temperature == 0.0
    assert request.response_format is not None
    assert request.response_format.name == "ContextSummary"
    assert request.messages[0].role is MessageRole.SYSTEM
    assert "compactor" in request.messages[0].content.lower() or "summar" in (
        request.messages[0].content.lower()
    )
    assert request.messages[1].content == TRANSCRIPT


async def test_an_invented_file_is_dropped_rather_than_passed_on() -> None:
    backend = FakeBackend(
        summary_json(
            files_touched=[
                "src/niharness/domain/gate.py",
                "src/niharness/adapters/qdrant/store.py",
            ],
        ),
    )
    summary = await ModelSummarizer(backend, "utility").summarize(TRANSCRIPT)
    assert summary.files_touched == ("src/niharness/domain/gate.py",)


async def test_prose_instead_of_json_is_an_error_the_caller_can_see() -> None:
    backend = FakeBackend("Sure! Here is a summary of the conversation so far.")
    with pytest.raises(ValueError, match="validation error"):
        await ModelSummarizer(backend, "utility").summarize(TRANSCRIPT)


async def test_the_policy_layer_is_part_of_the_compactor_prompt() -> None:
    backend = FakeBackend(summary_json())
    await ModelSummarizer(backend, "utility").summarize(TRANSCRIPT)
    system = backend.requests[0].messages[0].content
    assert "NOTES.md" in system or "policy" in system.lower()
    assert "SPDX" not in system

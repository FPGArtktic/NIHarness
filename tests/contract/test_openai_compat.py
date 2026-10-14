# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The inference adapter, driven by recorded responses. No live backend."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from niharness.adapters.openai_compat import AirGapViolationError, OpenAiCompatBackend
from niharness.adapters.openai_compat.client import BackendProtocolError, require_local
from niharness.domain.messages import (
    ChatRequest,
    Message,
    MessageRole,
    ResponseFormat,
    ToolChoice,
    ToolSchema,
)

BASE = "http://127.0.0.1:8080"
CASSETTES = Path(__file__).parent / "cassettes"


def cassette(name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((CASSETTES / name).read_text(encoding="utf-8"))
    return loaded


def backend(
    *,
    supports_grammar: bool = False,
    supports_reasoning: bool = False,
) -> OpenAiCompatBackend:
    return OpenAiCompatBackend(
        BASE,
        "Qwen3-Coder-30B-A3B-Instruct",
        context_window=65_536,
        supports_grammar=supports_grammar,
        supports_reasoning=supports_reasoning,
    )


def request(**kwargs: object) -> ChatRequest:
    defaults: dict[str, object] = {
        "model": "Qwen3-Coder-30B-A3B-Instruct",
        "messages": (Message(role=MessageRole.SYSTEM, content="policy"),),
    }
    return ChatRequest.model_validate(defaults | kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8080",
        "https://api.openai.com/v1",
        "ftp://127.0.0.1",
        "http://10.0.0.5",
    ],
)
def test_a_non_local_backend_is_refused(url: str) -> None:
    with pytest.raises(AirGapViolationError):
        OpenAiCompatBackend(url, "m", context_window=4_096)


@pytest.mark.parametrize(
    "url", ["http://localhost:8080", "http://127.0.0.1:8081/", "http://[::1]:9"]
)
def test_local_urls_are_accepted_and_normalised(url: str) -> None:
    assert not require_local(url).endswith("/")


async def test_info_reports_the_configured_capabilities() -> None:
    info = await backend(supports_grammar=True, supports_reasoning=True).info()
    assert info.model == "Qwen3-Coder-30B-A3B-Instruct"
    assert info.context_window == 65_536
    assert info.supports_grammar
    assert info.base_url == BASE


@respx.mock
async def test_tool_calls_and_reasoning_are_parsed_separately() -> None:
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=cassette("chat_tool_call.json")),
    )
    response = await backend().chat(request())
    assert route.called
    assert response.finish_reason == "tool_calls"
    assert response.content == ""
    assert response.reasoning_content.startswith("I should read the file")
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.call_id, call.name) == ("call_abc123", "read_file")
    assert json.loads(call.arguments_json)["path"].endswith("gate.py")
    assert response.usage.prompt_tokens == 1843
    assert response.usage.total == 1900


@respx.mock
async def test_a_final_answer_carries_no_tool_calls() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=cassette("chat_final.json")),
    )
    response = await backend().chat(request())
    assert response.tool_calls == ()
    assert json.loads(response.content)["dod"][0]["done"] is True


@respx.mock
async def test_the_request_payload_is_openai_shaped() -> None:
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=cassette("chat_final.json")),
    )
    await backend(supports_grammar=True).chat(
        request(
            tools=(
                ToolSchema(
                    name="read_file",
                    description="read a file",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            tool_choice=ToolChoice.NONE,
            temperature=0.1,
            seed=7,
            stop=("<|im_end|>",),
            response_format=ResponseFormat(
                name="FinalAnswer",
                json_schema={"type": "object"},
                grammar='root ::= "{"',
            ),
        ),
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent["messages"] == [{"role": "system", "content": "policy"}]
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert sent["tool_choice"] == "none"
    assert sent["stream"] is False
    assert sent["seed"] == 7
    assert sent["stop"] == ["<|im_end|>"]
    assert sent["response_format"]["json_schema"]["name"] == "FinalAnswer"
    assert sent["grammar"] == 'root ::= "{"'


@respx.mock
async def test_a_grammar_is_withheld_from_a_backend_that_cannot_use_it() -> None:
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=cassette("chat_final.json")),
    )
    await backend(supports_grammar=False).chat(
        request(
            response_format=ResponseFormat(
                name="Verdict",
                json_schema={"type": "object"},
                grammar='root ::= "{"',
            ),
        ),
    )
    assert "grammar" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_token_counts_come_from_the_backend() -> None:
    respx.post(f"{BASE}/tokenize").mock(
        return_value=httpx.Response(200, json=cassette("tokenize.json")),
    )
    assert await backend().count_text("hello world, this is a test") == 7


@respx.mock
async def test_messages_are_counted_through_the_chat_template() -> None:
    template = respx.post(f"{BASE}/apply-template").mock(
        return_value=httpx.Response(200, json=cassette("apply_template.json")),
    )
    tokenize = respx.post(f"{BASE}/tokenize").mock(
        return_value=httpx.Response(200, json=cassette("tokenize.json")),
    )
    count = await backend().count_messages(
        [
            Message(role=MessageRole.SYSTEM, content="policy"),
            Message(role=MessageRole.USER, content="the task"),
        ],
    )
    assert (count, template.called, tokenize.called) == (7, True, True)
    assert "<|im_start|>" in json.loads(tokenize.calls.last.request.content)["content"]


@respx.mock
async def test_a_backend_without_apply_template_still_counts() -> None:
    respx.post(f"{BASE}/apply-template").mock(return_value=httpx.Response(404))
    tokenize = respx.post(f"{BASE}/tokenize").mock(
        return_value=httpx.Response(200, json=cassette("tokenize.json")),
    )
    assert await backend().count_messages([Message(role=MessageRole.USER, content="x")]) == 7
    assert json.loads(tokenize.calls.last.request.content)["content"] == "user: x"


@respx.mock
async def test_http_errors_are_not_swallowed() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await backend().chat(request())


@respx.mock
async def test_an_unusable_body_is_reported_as_a_protocol_error() -> None:
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []}),
    )
    with pytest.raises(BackendProtocolError, match="no choices"):
        await backend().chat(request())


@respx.mock
async def test_a_tokenizer_without_tokens_is_reported() -> None:
    respx.post(f"{BASE}/tokenize").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(BackendProtocolError, match="token list"):
        await backend().count_text("x")

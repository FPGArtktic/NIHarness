# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""An OpenAI-compatible chat client over localhost HTTP.

This is the only inference code path in the harness. `llama-server` and
`bitnet.cpp` are both driven through it; their differences are configuration,
not branches. Any non-localhost base URL is refused at construction, because
the harness runs air-gapped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final
from urllib.parse import urlparse

import httpx
import structlog

from niharness.domain.identity import CallId
from niharness.domain.messages import (
    ChatRequest,
    ChatResponse,
    Message,
    MessageRole,
    ToolCallRequest,
    ToolSchema,
    Usage,
)
from niharness.ports.model_backend import BackendInfo

LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104
CHAT_PATH: Final = "/v1/chat/completions"
TOKENIZE_PATH: Final = "/tokenize"
TEMPLATE_PATH: Final = "/apply-template"

_log = structlog.get_logger(__name__)


class AirGapViolationError(RuntimeError):
    """Raised when a backend URL points anywhere but localhost."""


class BackendProtocolError(RuntimeError):
    """Raised when the backend answers something the harness cannot interpret."""


MAX_ERROR_BODY = 2_000
"""How much of a refusal's body is carried into the exception."""


def _checked(response: httpx.Response) -> None:
    """Raise with the server's own explanation when a request was refused.

    `raise_for_status` reports the status and the URL, and throws away the
    body - which is where a server says *what* it objected to. A backend that
    refuses a request is not a mystery to be guessed at from the status code:
    the body goes into the message, truncated, so the operator reads the reason
    instead of rediscovering it with curl.

    Raises:
        httpx.HTTPStatusError: when the response is an error, with the body.
    """
    if not response.is_error:
        return
    body = response.text.strip()
    if len(body) > MAX_ERROR_BODY:
        body = f"{body[:MAX_ERROR_BODY]}... [truncated]"
    message = (
        f"{response.status_code} from {response.request.method} {response.request.url}: "
        f"{body or '(no body)'}"
    )
    raise httpx.HTTPStatusError(message, request=response.request, response=response)


def require_local(base_url: str) -> str:
    """Return `base_url` unchanged, or refuse it.

    Args:
        base_url: the configured endpoint.

    Returns:
        The URL without a trailing slash.

    Raises:
        AirGapViolationError: when the host is not a loopback address. There is
            no configuration flag to switch this off.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip("[]")
    if parsed.scheme not in {"http", "https"} or host not in LOCAL_HOSTS:
        msg = f"backend must be served from localhost, got {base_url!r}"
        raise AirGapViolationError(msg)
    return base_url.rstrip("/")


class OpenAiCompatBackend:
    """A chat-completions and tokenizer client.

    Implements :class:`niharness.ports.model_backend.ModelBackend` and
    :class:`niharness.ports.tokenizer.Tokenizer`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        context_window: int,
        supports_grammar: bool = False,
        supports_reasoning: bool = False,
        reasoning_effort: str = "",
        timeout_seconds: float = 600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create a client for a local backend.

        Args:
            base_url: the backend root, which must be a localhost URL.
            model: the model name to send in requests.
            context_window: the window the backend was started with.
            supports_grammar: whether GBNF may be sent for structured output.
            supports_reasoning: whether responses may carry reasoning content.
            reasoning_effort: how long a reasoning model may think, when it
                takes the standard field. Empty leaves the server's default,
                which on a reasoning model is usually its longest setting -
                and the reasoning counts against the output budget, so a step
                can end with an empty answer and no explanation.
            timeout_seconds: request timeout; local generation can be slow.
            client: an injected HTTP client, which is how contract tests
                serve recorded responses.
        """
        self._base_url = require_local(base_url)
        self._model = model
        self._context_window = context_window
        self._supports_grammar = supports_grammar
        self._supports_reasoning = supports_reasoning
        self._reasoning_effort = reasoning_effort
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def info(self) -> BackendInfo:
        """Return what this backend is, without asking it anything."""
        return BackendInfo(
            model=self._model,
            context_window=self._context_window,
            supports_grammar=self._supports_grammar,
            supports_reasoning=self._supports_reasoning,
            base_url=self._base_url,
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run one completion and return the parsed response."""
        payload = self._request_payload(request)
        response = await self._client.post(CHAT_PATH, json=payload)
        _checked(response)
        parsed = self._parse_response(response.json())
        _log.debug(
            "model.response",
            model=request.model,
            finish_reason=parsed.finish_reason,
            completion_tokens=parsed.usage.completion_tokens,
            tool_calls=len(parsed.tool_calls),
        )
        return parsed

    async def count_text(self, text: str) -> int:
        """Return the backend's own token count for `text`."""
        response = await self._client.post(TOKENIZE_PATH, json={"content": text})
        _checked(response)
        body = response.json()
        tokens = body.get("tokens")
        if not isinstance(tokens, list):
            msg = f"{TOKENIZE_PATH} returned no token list"
            raise BackendProtocolError(msg)
        return len(tokens)

    async def count_messages(self, messages: Sequence[Message]) -> int:
        """Return the token count of a message list.

        The backend renders the messages with its own chat template when it
        offers `/apply-template`; otherwise the serialised messages are
        tokenised directly. Either way the count comes from the tokenizer, not
        from a character heuristic.
        """
        rendered = await self._apply_template(messages)
        return await self.count_text(rendered)

    async def _apply_template(self, messages: Sequence[Message]) -> str:
        """Render `messages` with the backend's chat template when possible."""
        payload = {"messages": [self._message_payload(message) for message in messages]}
        response = await self._client.post(TEMPLATE_PATH, json=payload)
        if response.status_code == httpx.codes.NOT_FOUND:
            return "\n".join(f"{m.role.value}: {m.content}" for m in messages)
        _checked(response)
        prompt = response.json().get("prompt")
        if not isinstance(prompt, str):
            msg = f"{TEMPLATE_PATH} returned no prompt"
            raise BackendProtocolError(msg)
        return prompt

    # ------------------------------------------------------------- serialisation

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        """Convert one harness message into the wire format."""
        payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.role is MessageRole.TOOL and message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name:
            payload["name"] = message.name
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments_json},
                }
                for call in message.tool_calls
            ]
        return payload

    @staticmethod
    def _tool_payload(tool: ToolSchema) -> dict[str, Any]:
        """Convert one tool schema into the wire format."""
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _request_payload(self, request: ChatRequest) -> dict[str, Any]:
        """Build the request body, including structured-output constraints."""
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message_payload(message) for message in request.messages],
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if self._reasoning_effort:
            # A reasoning model spends the output budget on thinking before it
            # answers, so on a slow host the effort is a real setting and not
            # a preference. `reasoning_effort` is the standard field for it.
            payload["reasoning_effort"] = self._reasoning_effort
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.tools:
            payload["tools"] = [self._tool_payload(tool) for tool in request.tools]
            payload["tool_choice"] = request.tool_choice.value
        if request.response_format is not None:
            fmt = request.response_format
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": fmt.name, "schema": fmt.json_schema, "strict": True},
            }
            if fmt.grammar and self._supports_grammar:
                payload["grammar"] = fmt.grammar
        return payload

    @staticmethod
    def _parse_tool_calls(raw: object) -> tuple[ToolCallRequest, ...]:
        """Parse the tool calls of a response message."""
        if not isinstance(raw, list):
            return ()
        calls: list[ToolCallRequest] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            function = entry.get("function")
            if not isinstance(function, dict):
                continue
            calls.append(
                ToolCallRequest(
                    call_id=CallId(str(entry.get("id") or f"call_{index}")),
                    name=str(function.get("name", "")),
                    arguments_json=str(function.get("arguments", "{}")),
                ),
            )
        return tuple(calls)

    def _parse_response(self, body: Any) -> ChatResponse:  # noqa: ANN401 - decoded JSON
        """Parse a chat-completions body into a :class:`ChatResponse`."""
        if not isinstance(body, dict):
            msg = "response body is not an object"
            raise BackendProtocolError(msg)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = "response carries no choices"
            raise BackendProtocolError(msg)
        choice = choices[0]
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        usage_raw = body.get("usage", {})
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0))
            if isinstance(usage_raw, dict)
            else 0,
            completion_tokens=(
                int(usage_raw.get("completion_tokens", 0)) if isinstance(usage_raw, dict) else 0
            ),
        )
        return ChatResponse(
            content=str(message.get("content") or ""),
            reasoning_content=str(message.get("reasoning_content") or ""),
            tool_calls=self._parse_tool_calls(message.get("tool_calls")),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=usage,
        )

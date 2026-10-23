# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Embeddings over the same OpenAI-compatible surface as inference.

`/v1/embeddings` is served by llama-server, by ollama and by anything else that
speaks the protocol, so the harness needs no embedding-specific backend: the
difference between bge-m3 on a GPU and a BitNet embedding model on a CPU-only
host is two lines of profile TOML.

The request is batched because the cost of a round trip dominates the cost of a
short text, and the response is reordered by its own `index` field rather than
trusted to arrive in order - the protocol says what the index is for, and a
retrieval index built from vectors paired with the wrong text fails silently,
which is the worst way for it to fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import httpx
import structlog

from niharness.adapters.local_http import checked, require_local
from niharness.adapters.openai_compat.client import BackendProtocolError
from niharness.ports.embedder import Vector

EMBEDDINGS_PATH: Final = "/v1/embeddings"
DEFAULT_BATCH = 16
"""How many texts go in one request. Large enough to amortise the round trip."""

_log = structlog.get_logger(__name__)


class EmbeddingWidthError(BackendProtocolError):
    """Raised when the model's vectors are not the width the harness declared.

    This is a configuration mistake, not a transient fault: the collection was
    created at the declared width and will refuse every vector. Naming both
    numbers and the model is the whole content of the fix.
    """

    def __init__(self, model: str, declared: int, actual: int) -> None:
        """Record the mismatch in a message an operator can act on."""
        super().__init__(
            f"{model} returned {actual}-dimensional vectors, but the configuration "
            f"declares {declared} (vector.embedding_dimensions). Fix the number, or "
            f"point [models.embeddings] at a model of that width.",
        )
        self.model = model
        self.declared = declared
        self.actual = actual


class OpenAiCompatEmbedder:
    """An :class:`niharness.ports.embedder.Embedder` over localhost HTTP."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        dimensions: int,
        timeout_seconds: float = 120.0,
        batch_size: int = DEFAULT_BATCH,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create an embedding client for a local backend.

        Args:
            base_url: the backend root, which must be a localhost URL.
            model: the embedding model's name.
            dimensions: the width the vector store was built at.
            timeout_seconds: request timeout. Embedding a batch on a CPU-only
                host is slow but not generation-slow.
            batch_size: how many texts per request.
            client: an injected HTTP client, which is how contract tests serve
                recorded responses.

        Raises:
            AirGapViolationError: when `base_url` is not a loopback address.
        """
        self._base_url = require_local(base_url)
        self._model = model
        self._dimensions = dimensions
        self._batch_size = max(1, batch_size)
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
        )

    @property
    def dimensions(self) -> int:
        """The width every vector is checked against."""
        return self._dimensions

    @property
    def model(self) -> str:
        """Which model is being asked. Reported by `nih tools`."""
        return self._model

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """Embed each text, in order, one request per batch.

        Raises:
            EmbeddingWidthError: when a vector is not `dimensions` wide.
            BackendProtocolError: when the response is not an embedding list.
        """
        if not texts:
            return ()
        vectors: list[Vector] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(await self._batch(batch))
        _log.debug("embeddings.done", model=self._model, texts=len(texts))
        return tuple(vectors)

    async def _batch(self, batch: list[str]) -> tuple[Vector, ...]:
        """Embed one batch and return its vectors in the order requested."""
        response = await self._client.post(
            EMBEDDINGS_PATH,
            json={"model": self._model, "input": batch},
        )
        checked(response)
        return self._parse(response.json(), len(batch))

    def _parse(self, body: Any, expected: int) -> tuple[Vector, ...]:  # noqa: ANN401 - decoded JSON
        """Turn one response into vectors, ordered by the server's own `index`.

        Raises:
            EmbeddingWidthError: when a vector is not `dimensions` wide.
            BackendProtocolError: when the shape is not what the protocol says.
        """
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected:
            got = len(data) if isinstance(data, list) else type(data).__name__
            msg = f"{EMBEDDINGS_PATH} returned {got} embeddings for {expected} inputs"
            raise BackendProtocolError(msg)
        ordered: list[Vector] = []
        for position, entry in enumerate(sorted(data, key=lambda item: _index_of(item))):
            vector = entry.get("embedding") if isinstance(entry, dict) else None
            if not isinstance(vector, list) or not all(
                isinstance(value, int | float) for value in vector
            ):
                msg = f"{EMBEDDINGS_PATH} entry {position} carries no float list"
                raise BackendProtocolError(msg)
            if len(vector) != self._dimensions:
                raise EmbeddingWidthError(self._model, self._dimensions, len(vector))
            ordered.append(tuple(float(value) for value in vector))
        return tuple(ordered)


def _index_of(entry: object) -> int:
    """Return an entry's `index`, or a large number when it has none.

    A server that omits the field leaves the order it sent, which is the only
    thing left to go on.
    """
    if isinstance(entry, dict):
        index = entry.get("index")
        if isinstance(index, int):
            return index
    return 1 << 31

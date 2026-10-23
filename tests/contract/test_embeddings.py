# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The embedding adapter, driven by recorded responses. No live backend."""

import httpx
import pytest
import respx

from niharness.adapters.openai_compat import (
    AirGapViolationError,
    EmbeddingWidthError,
    OpenAiCompatEmbedder,
)
from niharness.adapters.openai_compat.client import BackendProtocolError

BASE = "http://127.0.0.1:8082"
URL = f"{BASE}/v1/embeddings"


def embedder(*, dimensions: int = 4, batch_size: int = 16) -> OpenAiCompatEmbedder:
    return OpenAiCompatEmbedder(
        BASE,
        "bge-m3",
        dimensions=dimensions,
        batch_size=batch_size,
    )


def entry(index: int, value: float, *, dimensions: int = 4) -> dict[str, object]:
    return {"object": "embedding", "index": index, "embedding": [value] * dimensions}


@pytest.mark.anyio
async def test_a_text_becomes_a_vector_of_the_declared_width() -> None:
    async with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": [entry(0, 0.5)], "model": "bge-m3"}),
        )
        vectors = await embedder().embed(["one"])
    assert vectors == ((0.5, 0.5, 0.5, 0.5),)


@pytest.mark.anyio
async def test_vectors_come_back_in_the_order_requested_not_the_order_sent() -> None:
    """The protocol has an `index` field; a server is entitled to reorder.

    A vector paired with the wrong text produces an index that retrieves
    plausible nonsense and never raises, so this is checked rather than
    assumed.
    """
    async with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [entry(2, 0.3), entry(0, 0.1), entry(1, 0.2)]},
            ),
        )
        vectors = await embedder().embed(["first", "second", "third"])
    assert [vector[0] for vector in vectors] == [0.1, 0.2, 0.3]


@pytest.mark.anyio
async def test_a_server_that_omits_the_index_keeps_the_order_it_sent() -> None:
    async with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"embedding": [0.7] * 4}, {"embedding": [0.8] * 4}]},
            ),
        )
        vectors = await embedder().embed(["a", "b"])
    assert [vector[0] for vector in vectors] == [0.7, 0.8]


@pytest.mark.anyio
async def test_texts_are_batched_and_every_batch_is_one_request() -> None:
    async with respx.mock:
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(200, json={"data": [entry(0, 0.1), entry(1, 0.2)]}),
                httpx.Response(200, json={"data": [entry(0, 0.3)]}),
            ],
        )
        vectors = await embedder(batch_size=2).embed(["a", "b", "c"])
    assert route.call_count == 2
    assert [vector[0] for vector in vectors] == [0.1, 0.2, 0.3]


@pytest.mark.anyio
async def test_nothing_to_embed_asks_nothing() -> None:
    async with respx.mock:
        route = respx.post(URL)
        assert await embedder().embed([]) == ()
    assert route.call_count == 0


@pytest.mark.anyio
async def test_a_vector_of_the_wrong_width_names_both_numbers() -> None:
    """The collection was built at the declared width and would refuse every vector."""
    async with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": [entry(0, 0.5, dimensions=768)]}),
        )
        with pytest.raises(EmbeddingWidthError) as caught:
            await embedder(dimensions=1024).embed(["one"])
    assert caught.value.declared == 1024
    assert caught.value.actual == 768
    assert "bge-m3" in str(caught.value)
    assert "embedding_dimensions" in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {"data": []},
        {"data": [entry(0, 0.1), entry(1, 0.2)]},
        {"data": [{"index": 0}]},
        {"data": [{"index": 0, "embedding": "not a list"}]},
        {"object": "list"},
    ],
)
async def test_a_response_that_is_not_an_embedding_list_is_refused(
    body: dict[str, object],
) -> None:
    async with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(BackendProtocolError):
            await embedder().embed(["one"])


@pytest.mark.anyio
async def test_a_refusal_carries_the_servers_own_explanation() -> None:
    async with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(400, text="model bge-m3 is not loaded"),
        )
        with pytest.raises(httpx.HTTPStatusError, match="not loaded"):
            await embedder().embed(["one"])


def test_an_embedding_model_off_this_host_is_refused() -> None:
    with pytest.raises(AirGapViolationError):
        OpenAiCompatEmbedder("http://embeddings.example.com", "bge-m3", dimensions=1024)

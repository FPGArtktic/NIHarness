# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Qdrant adapter, driven by recorded responses. No live store."""

from collections.abc import Sequence

import httpx
import pytest
import respx

from niharness.adapters.local_http import AirGapViolationError
from niharness.adapters.qdrant import QdrantStore, point_id
from niharness.domain.rag import Chunk, ChunkKind
from niharness.ports.embedder import Vector
from niharness.ports.vector_store import (
    CollectionMissingError,
    CollectionWidthError,
    StoreUnavailableError,
    VectorStore,
)

BASE = "http://127.0.0.1:6333"
DIGEST = "f" * 64
WIDTH = 4


class FakeEmbedder:
    """Embeds a text as its length, so a vector is traceable to its input."""

    def __init__(self, dimensions: int = WIDTH) -> None:
        self._dimensions = dimensions
        self.asked: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        self.asked.append(list(texts))
        return tuple((float(len(text)),) * self._dimensions for text in texts)


def chunk(index: int, text: str = "body", **kwargs: object) -> Chunk:
    fields: dict[str, object] = {
        "id": f"src/a.py:{DIGEST}:{index}",
        "kind": ChunkKind.CODE,
        "path": "src/a.py",
        "text": text,
        "file_digest": DIGEST,
    }
    fields.update(kwargs)
    return Chunk.model_validate(fields)


def store(embedder: FakeEmbedder | None = None, **kwargs: object) -> QdrantStore:
    return QdrantStore(BASE, embedder or FakeEmbedder(), **kwargs)  # type: ignore[arg-type]


def described(size: int = WIDTH, points: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": {
                "config": {"params": {"vectors": {"size": size, "distance": "Cosine"}}},
                "points_count": points,
            },
            "status": "ok",
        },
    )


MISSING = httpx.Response(404, json={"status": {"error": "Not found"}})


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(store(), VectorStore)


def test_a_store_off_this_host_is_refused() -> None:
    with pytest.raises(AirGapViolationError):
        QdrantStore("http://qdrant.example.com", FakeEmbedder())


def test_a_point_id_is_a_uuid_derived_from_the_chunk_id() -> None:
    """Qdrant takes an integer or a UUID; a chunk id is neither.

    The mapping has to be deterministic, or re-indexing an unchanged chunk
    would add a second copy of it instead of replacing one.
    """
    assert point_id("src/a.py:abc:0") == point_id("src/a.py:abc:0")
    assert point_id("src/a.py:abc:0") != point_id("src/a.py:abc:1")
    assert len(point_id("x")) == 36


@pytest.mark.anyio
async def test_a_collection_that_is_absent_is_created_at_the_embedders_width() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=MISSING)
        created = respx.put(f"{BASE}/collections/code").mock(
            return_value=httpx.Response(200, json={"result": True, "status": "ok"}),
        )
        await store(FakeEmbedder(dimensions=1024)).ensure_collection("code")
    assert created.calls.last.request.read() == (
        b'{"vectors":{"size":1024,"distance":"Cosine"}}'
    )


@pytest.mark.anyio
async def test_a_collection_that_exists_at_the_right_width_is_left_alone() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described())
        created = respx.put(f"{BASE}/collections/code")
        await store().ensure_collection("code")
    assert created.call_count == 0


@pytest.mark.anyio
async def test_a_collection_built_for_another_model_names_both_widths() -> None:
    """Qdrant would refuse every vector with a message about dimensions."""
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described(size=768))
        with pytest.raises(CollectionWidthError) as caught:
            await store(FakeEmbedder(dimensions=1024)).ensure_collection("code")
    assert caught.value.existing == 768
    assert caught.value.wanted == 1024
    assert "re-index" in str(caught.value).lower()


@pytest.mark.anyio
async def test_upsert_embeds_the_text_and_sends_the_chunk_as_the_payload() -> None:
    embedder = FakeEmbedder()
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described())
        written = respx.put(f"{BASE}/collections/code/points").mock(
            return_value=httpx.Response(200, json={"result": {"status": "completed"}}),
        )
        count = await store(embedder).upsert(
            "code",
            [chunk(0, "def one(): ...", start_line=1, end_line=2, symbol="one")],
        )
    assert count == 1
    assert embedder.asked == [["def one(): ..."]]
    sent = written.calls.last.request
    assert sent.url.params["wait"] == "true"
    point = httpx.Response(200, content=sent.read()).json()["points"][0]
    assert point["id"] == point_id(f"src/a.py:{DIGEST}:0")
    assert point["vector"] == [14.0] * WIDTH
    assert point["payload"]["id"] == f"src/a.py:{DIGEST}:0"
    assert point["payload"]["symbol"] == "one"
    assert "first_page" not in point["payload"], "a code chunk has no pages to store"


@pytest.mark.anyio
async def test_upsert_batches_so_one_request_stays_a_reasonable_size() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described())
        written = respx.put(f"{BASE}/collections/code/points").mock(
            return_value=httpx.Response(200, json={"result": {"status": "completed"}}),
        )
        count = await store(batch_size=2).upsert("code", [chunk(index) for index in range(5)])
    assert (count, written.call_count) == (5, 3)


@pytest.mark.anyio
async def test_nothing_to_index_asks_nothing() -> None:
    async with respx.mock:
        route = respx.get(f"{BASE}/collections/code")
        assert await store().upsert("code", []) == 0
    assert route.call_count == 0


@pytest.mark.anyio
async def test_a_query_rebuilds_the_chunks_and_their_scores() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/docs").mock(return_value=described())
        respx.post(f"{BASE}/collections/docs/points/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": point_id("a"),
                                "score": 0.81,
                                "payload": chunk(
                                    0,
                                    "napiecie 1.8 V",
                                    kind=ChunkKind.DOC,
                                    path="datasheet.pdf",
                                    first_page=12,
                                    last_page=13,
                                    provenance="ocr",
                                ).model_dump(mode="json", exclude_none=True),
                            },
                        ],
                    },
                    "status": "ok",
                },
            ),
        )
        hits = await store().query("docs", "what voltage", 5)
    assert len(hits) == 1
    assert hits[0].score == pytest.approx(0.81)
    assert hits[0].chunk.citation() == "datasheet.pdf pp. 12-13 (OCR)"


@pytest.mark.anyio
async def test_a_point_written_by_an_older_schema_is_skipped_not_fatal() -> None:
    """Retrieval happens in the middle of a step; one stale point must not end it."""
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described())
        respx.post(f"{BASE}/collections/code/points/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {"id": point_id("stale"), "score": 0.9, "payload": {"nonsense": 1}},
                            {
                                "id": point_id("good"),
                                "score": 0.4,
                                "payload": chunk(1).model_dump(mode="json", exclude_none=True),
                            },
                        ],
                    },
                },
            ),
        )
        hits = await store().query("code", "anything", 5)
    assert [hit.chunk.id for hit in hits] == [f"src/a.py:{DIGEST}:1"]


@pytest.mark.anyio
async def test_querying_a_collection_that_does_not_exist_says_so() -> None:
    """A query that matched nothing differs from a collection nobody built."""
    async with respx.mock:
        respx.get(f"{BASE}/collections/run-7").mock(return_value=MISSING)
        with pytest.raises(CollectionMissingError, match="run-7"):
            await store().query("run-7", "anything", 5)


@pytest.mark.anyio
async def test_points_counts_what_is_indexed_and_none_when_there_is_no_collection() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/code").mock(return_value=described(points=412))
        respx.get(f"{BASE}/collections/docs").mock(return_value=MISSING)
        assert await store().points("code") == 412
        assert await store().points("docs") is None


@pytest.mark.anyio
async def test_forgetting_a_document_deletes_by_its_path_not_by_its_ids() -> None:
    """A revision's chunk ids are not known here; only that they carried the path.

    Chunk ids come from the content, so a revised document's chunks are new
    points and the previous version's would otherwise stay indexed and be
    cited as current.
    """
    async with respx.mock:
        respx.get(f"{BASE}/collections/docs").mock(
            side_effect=[described(points=7), described(points=4)],
        )
        deleted = respx.post(f"{BASE}/collections/docs/points/delete").mock(
            return_value=httpx.Response(200, json={"result": {"status": "completed"}}),
        )
        removed = await store().forget("docs", "datasheet.pdf")
    assert removed == 3
    sent = httpx.Response(200, content=deleted.calls.last.request.read()).json()
    assert sent == {"filter": {"must": [{"key": "path", "match": {"value": "datasheet.pdf"}}]}}


@pytest.mark.anyio
async def test_forgetting_in_a_collection_that_is_empty_or_absent_deletes_nothing() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/collections/docs").mock(return_value=MISSING)
        respx.get(f"{BASE}/collections/code").mock(return_value=described(points=0))
        deleted = respx.post(f"{BASE}/collections/docs/points/delete")
        assert await store().forget("docs", "datasheet.pdf") == 0
        assert await store().forget("code", "datasheet.pdf") == 0
    assert deleted.call_count == 0


@pytest.mark.anyio
async def test_dropping_a_collection_that_is_not_there_is_not_an_error() -> None:
    async with respx.mock:
        respx.delete(f"{BASE}/collections/run-7").mock(return_value=MISSING)
        await store().drop("run-7")


@pytest.mark.anyio
async def test_a_store_that_is_not_running_is_reported_as_such() -> None:
    """The fix is to start something, which the message has to say."""
    async with respx.mock:
        respx.get(f"{BASE}/").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(StoreUnavailableError, match="no vector store at"):
            await store().version()


@pytest.mark.anyio
async def test_the_version_and_collection_listing_come_from_the_store() -> None:
    async with respx.mock:
        respx.get(f"{BASE}/").mock(
            return_value=httpx.Response(200, json={"title": "qdrant", "version": "1.19.0"}),
        )
        respx.get(f"{BASE}/collections").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"collections": [{"name": "docs"}, {"name": "code"}]}},
            ),
        )
        assert await store().version() == "1.19.0"
        assert await store().collections() == ("code", "docs")

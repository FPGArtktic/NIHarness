# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`rag_query`: what the model is offered, and what it gets back.

Everything here runs against a fake store. What is being tested is the policy
around retrieval - which collections a query reaches, what an absent one means,
and that a store failure is a red tool result rather than a dead step - not
whether Qdrant works.
"""

from collections.abc import Sequence

import pytest

from niharness.adapters.retrieval import RetrievalTools
from niharness.domain.identity import CallId
from niharness.domain.rag import Chunk, ChunkKind, Hit
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tools import AcceptedCall, RagQueryArgs, ToolName
from niharness.ports.vector_store import (
    CollectionMissingError,
    StoreUnavailableError,
    VectorStoreError,
)

DIGEST = "d" * 64
IMPLEMENTER = DEFAULT_ROLES[Role.IMPLEMENTER].allowlist


def chunk(identifier: str, text: str = "body", **kwargs: object) -> Chunk:
    fields: dict[str, object] = {
        "id": identifier,
        "kind": ChunkKind.CODE,
        "path": "src/a.py",
        "text": text,
        "file_digest": DIGEST,
    }
    fields.update(kwargs)
    return Chunk.model_validate(fields)


class FakeStore:
    """A store whose collections are declared up front."""

    def __init__(
        self,
        contents: dict[str, tuple[Hit, ...]] | None = None,
        *,
        failure: VectorStoreError | None = None,
    ) -> None:
        self._contents = contents or {}
        self._failure = failure
        self.asked: list[tuple[str, str, int]] = []

    async def ensure_collection(self, name: str) -> None:  # pragma: no cover - unused here
        self._contents.setdefault(name, ())

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        raise AssertionError("rag_query must never write")

    async def query(self, collection: str, text: str, top_k: int) -> tuple[Hit, ...]:
        self.asked.append((collection, text, top_k))
        if self._failure is not None:
            raise self._failure
        if collection not in self._contents:
            raise CollectionMissingError(collection)
        return self._contents[collection][:top_k]

    async def forget(self, collection: str, path: str) -> int:
        raise AssertionError("rag_query must never write")

    async def drop(self, collection: str) -> None:  # pragma: no cover - unused here
        self._contents.pop(collection, None)


def tools(store: FakeStore, *, run: str = "run-7") -> RetrievalTools:
    return RetrievalTools(
        store,
        code_collection="code",
        doc_collection="docs",
        run_collection=run,
    )


def call(**arguments: object) -> AcceptedCall:
    return AcceptedCall(
        call_id=CallId("c1"),
        tool=ToolName.RAG_QUERY.value,
        args=RagQueryArgs.model_validate({"query": "what voltage", **arguments}),
    )


@pytest.mark.anyio
async def test_rag_query_is_offered_to_a_role_that_may_call_it() -> None:
    schemas = await tools(FakeStore()).descriptors(IMPLEMENTER)
    assert [schema.name for schema in schemas] == ["rag_query"]
    assert schemas[0].parameters["properties"].keys() == {"query", "top_k", "collection"}
    assert "citation" in schemas[0].description


@pytest.mark.anyio
async def test_a_role_without_rag_query_is_offered_nothing() -> None:
    """The compactor has no tools at all; the provider must not widen that."""
    compactor = DEFAULT_ROLES[Role.COMPACTOR].allowlist
    assert await tools(FakeStore()).descriptors(compactor) == ()


@pytest.mark.anyio
async def test_a_query_searches_every_collection_and_merges_what_it_finds() -> None:
    store = FakeStore(
        {
            "code": (Hit(chunk=chunk("a", "gate runs lint"), score=0.4),),
            "docs": (Hit(chunk=chunk("b", "1.8 V"), score=0.9),),
            "run-7": (),
        },
    )
    result = await tools(store).call(call(top_k=3))
    assert result.ok
    assert [asked[0] for asked in store.asked] == ["code", "docs", "run-7"]
    assert result.content.startswith("2 match(es) in code, docs, run-7:")
    assert result.content.index("1.8 V") < result.content.index("gate runs lint")
    assert result.summary == "rag_query: 2 match(es)"


@pytest.mark.anyio
async def test_a_collection_nothing_was_indexed_into_is_skipped_and_named() -> None:
    """A run's own collection does not exist until compaction writes to it.

    Refusing the whole query then would make `rag_query` useless for most of a
    run, so it is skipped - but silently skipping it would make an empty answer
    indistinguishable from an exhaustive one.
    """
    store = FakeStore({"code": (Hit(chunk=chunk("a"), score=0.5),)})
    result = await tools(store).call(call())
    assert result.ok
    assert "1 match(es) in code:" in result.content
    assert "not indexed yet, so not searched: docs, run-7" in result.content


@pytest.mark.anyio
async def test_a_collection_asked_for_by_name_that_is_absent_is_reported() -> None:
    """Asking for one by name and getting nothing back is usually a typo."""
    result = await tools(FakeStore({"code": ()})).call(call(collection="codes"))
    assert result.ok is False
    assert "no collection 'codes'" in result.content
    assert "code, docs, run-7" in result.content, "say what there is instead"


@pytest.mark.anyio
async def test_a_store_that_fell_over_is_a_red_result_not_a_dead_step() -> None:
    """The model is told plainly and the step ends like any other dead end."""
    store = FakeStore(failure=StoreUnavailableError("http://127.0.0.1:6333", "refused"))
    result = await tools(store).call(call())
    assert result.ok is False
    assert "no vector store at http://127.0.0.1:6333" in result.content


@pytest.mark.anyio
async def test_a_run_without_its_own_collection_searches_the_rest() -> None:
    store = FakeStore({"code": (), "docs": ()})
    result = await tools(store, run="").call(call())
    assert [asked[0] for asked in store.asked] == ["code", "docs"]
    assert result.content == "no matches in code, docs"

# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Building the retrieval stack from configuration.

Three commands need the same three pieces wired the same way - `nih ingest`
writes to the store, `nih implement` offers `rag_query` over it, `nih tools`
reports whether it is there - so the wiring lives here rather than three times.

Retrieval is optional, and that shapes the interface. A project with no
embedding model configured, or no vector store running, is not broken: it runs
without `rag_query`, which simply is not offered to the model. So the result of
asking for retrieval is either a working stack or a sentence saying why there
is none, never an exception that ends a run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from niharness.adapters.local_http import AirGapViolationError, explain
from niharness.adapters.openai_compat import OpenAiCompatEmbedder
from niharness.adapters.openai_compat.client import BackendProtocolError
from niharness.adapters.qdrant import QdrantStore
from niharness.adapters.retrieval import RetrievalTools
from niharness.config import HarnessConfig
from niharness.ports.vector_store import VectorStoreError

NO_EMBEDDING_MODEL = (
    "no [models.embeddings] in the profile, so nothing can be embedded or searched"
)


@dataclass(frozen=True)
class Retrieval:
    """A working retrieval stack, or the reason there is not one.

    Exactly one of `store` and `why` is meaningful: a stack that reports a
    reason has no store, and one with a store has nothing to explain.
    """

    store: QdrantStore | None
    embedder: OpenAiCompatEmbedder | None
    why: str = ""

    @property
    def ready(self) -> bool:
        """Whether the store can be used."""
        return self.store is not None

    def tools(self, config: HarnessConfig, *, run_id: str = "") -> RetrievalTools | None:
        """Return the `rag_query` provider for this stack, or None when there is none.

        Args:
            config: the collections to search come from `[vector]`.
            run_id: the run whose own collection joins the default search set.
        """
        if self.store is None:
            return None
        return RetrievalTools(
            self.store,
            code_collection=config.vector.code_collection,
            doc_collection=config.vector.doc_collection,
            run_collection=config.vector.run_collection(run_id) if run_id else "",
            limits=config.tools,
        )


@asynccontextmanager
async def open_retrieval(
    config: HarnessConfig, *, probe: bool = True
) -> AsyncIterator[Retrieval]:
    """Yield a retrieval stack built from `config`, closing it afterwards.

    Both halves are probed, not just the store. A running store with no
    embedding model behind it accepts `ensure_collection` and fails on the
    first query, which is the worst moment to find out; embedding one short
    string here also checks the model's width against the one the collections
    were built at.

    Args:
        config: `[models.embeddings]` names the model, `[vector]` the store.
        probe: check both halves first, so an unreachable one is reported once,
            here, instead of at the first query inside a step.

    Yields:
        A stack that is either ready or carries the reason it is not.
    """
    model = config.models.embeddings
    if model is None:
        yield Retrieval(store=None, embedder=None, why=NO_EMBEDDING_MODEL)
        return
    try:
        embedder = OpenAiCompatEmbedder(
            model.base_url,
            model.name,
            dimensions=config.vector.embedding_dimensions,
            timeout_seconds=model.request_timeout_seconds,
        )
        store = QdrantStore(config.vector.url, embedder)
    except AirGapViolationError as error:
        yield Retrieval(store=None, embedder=None, why=str(error))
        return
    try:
        if probe:
            await store.version()
            await embedder.embed(["probe"])
        yield Retrieval(store=store, embedder=embedder)
    except (VectorStoreError, BackendProtocolError, httpx.HTTPError) as error:
        yield Retrieval(
            store=None,
            embedder=None,
            why=explain(error) if isinstance(error, httpx.HTTPError) else str(error),
        )
    finally:
        await store.aclose()
        await embedder.aclose()

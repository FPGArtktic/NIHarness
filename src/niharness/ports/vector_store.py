# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The vector store port.

The store serves three purposes: code retrieval for every role, the documents
`nih ingest` read, and recovery of run context that compaction removed from the
window.

The failures live here rather than in an adapter because they are part of the
contract. A caller has to tell "nothing matched" from "nothing is indexed yet"
from "the store is not running", and it must be able to do so without knowing
whether the store behind the port is Qdrant.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from niharness.domain.rag import Chunk, Hit


class VectorStoreError(RuntimeError):
    """Raised when the vector store cannot do what was asked."""


class StoreUnavailableError(VectorStoreError):
    """Raised when the store cannot be reached at all.

    Distinct from a refusal: an operator reading this has to start the store,
    not fix a request.
    """

    def __init__(self, url: str, error: object) -> None:
        """Name the endpoint, because the fix is to start something there."""
        super().__init__(f"no vector store at {url}: {error}")
        self.url = url


class CollectionMissingError(VectorStoreError):
    """Raised when a collection was queried before anything was indexed into it.

    Not an empty result: "no such collection" is usually a typo, or a step
    reaching for an index that was never built. The caller decides which of
    those it is - the retrieval tool tolerates it in its default search set and
    reports it when the collection was named.
    """

    def __init__(self, collection: str) -> None:
        """Name the collection; the caller decides whether that is fatal."""
        super().__init__(f"no collection {collection!r} in the vector store")
        self.collection = collection


class CollectionWidthError(VectorStoreError):
    """Raised when a collection was built for a different embedding model.

    The store would refuse every vector with a message about dimensions.
    Saying it here names the collection and both widths, which is the whole
    fix: re-index, or point the configuration at the model the collection was
    built for.
    """

    def __init__(self, collection: str, existing: int, wanted: int) -> None:
        """Record both widths."""
        super().__init__(
            f"collection {collection!r} holds {existing}-dimensional vectors, but the "
            f"configured embedding model produces {wanted}. Drop and re-index it, or "
            f"configure the model it was built with.",
        )
        self.collection = collection
        self.existing = existing
        self.wanted = wanted


@runtime_checkable
class VectorStore(Protocol):
    """A collection-based similarity index over chunks."""

    async def ensure_collection(self, name: str) -> None:
        """Create the collection if it does not exist; a no-op when it does.

        Raises:
            CollectionWidthError: when it exists at another vector width.
            StoreUnavailableError: when the store cannot be reached.
        """
        ...

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Index `chunks`, replacing any with the same id. Returns how many were written.

        Raises:
            StoreUnavailableError: when the store cannot be reached.
        """
        ...

    async def query(self, collection: str, text: str, top_k: int) -> tuple[Hit, ...]:
        """Return the best `top_k` hits for `text`, highest score first.

        Raises:
            CollectionMissingError: when the collection does not exist.
            StoreUnavailableError: when the store cannot be reached.
        """
        ...

    async def forget(self, collection: str, path: str) -> int:
        """Remove everything indexed from `path`. Returns how many were removed.

        What makes re-indexing a *replacement*. Chunk ids are derived from the
        content, so a revised document's chunks get new ids and the previous
        version's would otherwise stay indexed forever - and be cited as
        current. The path is the one identity that survives a revision.

        A collection that does not exist removes nothing and is not an error:
        there is nothing to forget.

        Raises:
            StoreUnavailableError: when the store cannot be reached.
        """
        ...

    async def drop(self, collection: str) -> None:
        """Delete a collection, for example when a run is discarded.

        Raises:
            StoreUnavailableError: when the store cannot be reached.
        """
        ...

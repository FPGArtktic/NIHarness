# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The vector store port.

The store serves two purposes: code retrieval for every role, and recovery of
run context that compaction removed from the window.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from niharness.domain.rag import Chunk, Hit


@runtime_checkable
class VectorStore(Protocol):
    """A collection-based similarity index over chunks."""

    async def ensure_collection(self, name: str) -> None:
        """Create the collection if it does not exist; a no-op when it does."""
        ...

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Index `chunks`, replacing any with the same id. Returns how many were written."""
        ...

    async def query(self, collection: str, text: str, top_k: int) -> tuple[Hit, ...]:
        """Return the best `top_k` hits for `text`, highest score first."""
        ...

    async def drop(self, collection: str) -> None:
        """Delete a collection, for example when a run is discarded."""
        ...

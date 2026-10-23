# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Qdrant over its REST API.

There is no `qdrant-client` dependency here, and that is a decision rather than
an omission. The harness already speaks HTTP to localhost with `httpx` for
inference, the five operations the :class:`~niharness.ports.vector_store.VectorStore`
port needs are five endpoints, and every dependency has to be mirrored into the
wheelhouse for the air gap. A client library would buy argument validation the
port already performs.

Two details are worth knowing before changing this file.

Point ids. Qdrant accepts an unsigned integer or a UUID, and a chunk's id is
neither - it is `path:digest:index`, readable and stable. Each is mapped to a
UUIDv5 of itself, so re-indexing an unchanged chunk overwrites its point rather
than adding a second copy, and the original id travels in the payload so a
citation survives the round trip.

Missing collections. `query` refuses one that does not exist instead of
answering nothing, because "no results" and "no such collection" are different
facts and only one of them is a typo. Deciding which to tolerate belongs to the
caller: the retrieval tool skips a missing collection out of its default set
and reports one that was asked for by name.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

import httpx
import structlog
from pydantic import ValidationError

from niharness.adapters.local_http import checked, require_local
from niharness.domain.rag import Chunk, Hit
from niharness.ports.embedder import Embedder
from niharness.ports.vector_store import (
    CollectionMissingError,
    CollectionWidthError,
    StoreUnavailableError,
)

NAMESPACE: Final = uuid.UUID("6f0c6e2e-9b0f-5c3a-8e3f-1d9b7a4c2e51")
"""A fixed namespace, so a chunk's point id is the same on every host."""

DEFAULT_DISTANCE: Final = "Cosine"
"""bge-m3 and the BitNet embedding model both produce vectors meant for cosine."""

UPSERT_BATCH = 256
"""Points per request. Keeps one request's body in the low megabytes."""

_log = structlog.get_logger(__name__)


def point_id(chunk_id: str) -> str:
    """Return the Qdrant point id for a chunk id.

    Deterministic, so indexing the same chunk twice writes one point.
    """
    return str(uuid.uuid5(NAMESPACE, chunk_id))


class QdrantStore:
    """A :class:`niharness.ports.vector_store.VectorStore` over Qdrant's REST API."""

    def __init__(
        self,
        url: str,
        embedder: Embedder,
        *,
        distance: str = DEFAULT_DISTANCE,
        batch_size: int = UPSERT_BATCH,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Bind a store to a running Qdrant and an embedder.

        Args:
            url: where Qdrant listens; must be a localhost URL.
            embedder: turns text into vectors, and declares their width.
            distance: the metric new collections are created with.
            batch_size: points per upsert request.
            timeout_seconds: request timeout. Indexing a batch is fast;
                embedding it, on a CPU-only host, is not - but that happens
                before the request.
            client: an injected HTTP client, which is how contract tests serve
                recorded responses.

        Raises:
            AirGapViolationError: when `url` is not a loopback address.
        """
        self._url = require_local(url)
        self._embedder = embedder
        self._distance = distance
        self._batch_size = max(1, batch_size)
        self._client = client or httpx.AsyncClient(base_url=self._url, timeout=timeout_seconds)

    @property
    def url(self) -> str:
        """Where this store lives. Reported by `nih tools`."""
        return self._url

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def version(self) -> str:
        """Return what Qdrant says it is, which is also a reachability check.

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        body = await self._get("/")
        version = body.get("version") if isinstance(body, dict) else None
        return str(version) if version else "unknown"

    async def collections(self) -> tuple[str, ...]:
        """List the collections that exist, sorted.

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        body = await self._get("/collections")
        listing = _result(body).get("collections", [])
        names = [
            entry["name"]
            for entry in listing
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
        return tuple(sorted(names))

    async def ensure_collection(self, name: str) -> None:
        """Create `name` at the embedder's width, or check the one that exists.

        Raises:
            CollectionWidthError: when it exists at another width.
            StoreUnavailableError: when nothing answers.
        """
        existing = await self._collection_width(name)
        if existing is not None:
            if existing != self._embedder.dimensions:
                raise CollectionWidthError(name, existing, self._embedder.dimensions)
            return
        response = await self._request(
            "PUT",
            f"/collections/{name}",
            json={
                "vectors": {"size": self._embedder.dimensions, "distance": self._distance},
            },
        )
        checked(response)
        _log.info(
            "vector.collection_created",
            collection=name,
            dimensions=self._embedder.dimensions,
            distance=self._distance,
        )

    async def upsert(self, collection: str, chunks: Sequence[Chunk]) -> int:
        """Embed and index `chunks`, replacing any already stored under the same id.

        Returns:
            How many points were written.

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        if not chunks:
            return 0
        await self.ensure_collection(collection)
        written = 0
        for start in range(0, len(chunks), self._batch_size):
            batch = list(chunks[start : start + self._batch_size])
            vectors = await self._embedder.embed([chunk.text for chunk in batch])
            points = [
                {
                    "id": point_id(chunk.id),
                    "vector": list(vector),
                    "payload": chunk.model_dump(mode="json", exclude_none=True),
                }
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            response = await self._request(
                "PUT",
                f"/collections/{collection}/points",
                params={"wait": "true"},
                json={"points": points},
            )
            checked(response)
            written += len(points)
        _log.info("vector.upserted", collection=collection, points=written)
        return written

    async def query(self, collection: str, text: str, top_k: int) -> tuple[Hit, ...]:
        """Return the best `top_k` hits for `text`, highest score first.

        Raises:
            CollectionMissingError: when the collection does not exist.
            StoreUnavailableError: when nothing answers.
        """
        if await self._collection_width(collection) is None:
            raise CollectionMissingError(collection)
        vector = (await self._embedder.embed([text]))[0]
        response = await self._request(
            "POST",
            f"/collections/{collection}/points/query",
            json={"query": list(vector), "limit": top_k, "with_payload": True},
        )
        checked(response)
        return self._hits(response.json(), collection)

    async def points(self, collection: str) -> int | None:
        """Return how many points a collection holds, or None when it does not exist.

        What `nih tools` needs to tell "retrieval is configured" from
        "retrieval will find nothing because nothing was indexed".

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        info = await self._collection(collection)
        if info is None:
            return None
        count = info.get("points_count")
        return count if isinstance(count, int) else 0

    async def forget(self, collection: str, path: str) -> int:
        """Remove every point indexed from `path`, and say how many there were.

        A filtered delete rather than a list of ids, because the ids of the
        *previous* version of a document are not known here - only that they
        carried this path. Qdrant does not need a payload index to filter;
        for a corpus this size the scan is cheaper than maintaining one.

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        before = await self.points(collection)
        if not before:
            return 0
        response = await self._request(
            "POST",
            f"/collections/{collection}/points/delete",
            params={"wait": "true"},
            json={"filter": {"must": [{"key": "path", "match": {"value": path}}]}},
        )
        checked(response)
        removed = before - (await self.points(collection) or 0)
        if removed:
            _log.info("vector.forgot", collection=collection, path=path, points=removed)
        return removed

    async def drop(self, collection: str) -> None:
        """Delete a collection; a no-op when it is not there.

        Raises:
            StoreUnavailableError: when nothing answers.
        """
        response = await self._request("DELETE", f"/collections/{collection}")
        if response.status_code != httpx.codes.NOT_FOUND:
            checked(response)
        _log.info("vector.collection_dropped", collection=collection)

    # ------------------------------------------------------------------ internals

    def _hits(self, body: Any, collection: str) -> tuple[Hit, ...]:  # noqa: ANN401 - decoded JSON
        """Rebuild hits from a query response, skipping points we cannot read.

        A payload written by an older schema is reported and skipped rather
        than raised: retrieval is a convenience in the middle of a step, and
        one stale point should not end the step. Re-ingesting replaces it.
        """
        points = _result(body).get("points", [])
        hits: list[Hit] = []
        unreadable = 0
        for point in points if isinstance(points, list) else []:
            if not isinstance(point, dict):
                unreadable += 1
                continue
            try:
                chunk = Chunk.model_validate(point.get("payload") or {})
            except ValidationError:
                unreadable += 1
                continue
            hits.append(Hit(chunk=chunk, score=float(point.get("score", 0.0))))
        if unreadable:
            _log.warning("vector.payload_unreadable", collection=collection, points=unreadable)
        return tuple(hits)

    async def _collection_width(self, name: str) -> int | None:
        """Return an existing collection's vector width, or None when it is absent.

        None also covers a collection using named vectors, where there is no
        single width to compare: Qdrant will say so itself on the first upsert,
        and in its own terms.
        """
        info = await self._collection(name)
        if info is None:
            return None
        params = info.get("config", {}).get("params", {})
        size = params.get("vectors", {}).get("size") if isinstance(params, dict) else None
        return size if isinstance(size, int) else None

    async def _collection(self, name: str) -> dict[str, Any] | None:
        """Return a collection's description, or None when it does not exist."""
        response = await self._request("GET", f"/collections/{name}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        checked(response)
        return _result(response.json())

    async def _get(self, path: str) -> Any:  # noqa: ANN401 - decoded JSON
        """GET a path and return the decoded body."""
        response = await self._request("GET", path)
        checked(response)
        return response.json()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401 - httpx's own kwargs
        """Make one request, turning a dead endpoint into a named failure.

        Raises:
            StoreUnavailableError: when Qdrant cannot be reached.
        """
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise StoreUnavailableError(self._url, error) from error


def _result(body: object) -> dict[str, Any]:
    """Return a Qdrant response's `result` object, or an empty one."""
    if isinstance(body, dict):
        result = body.get("result")
        if isinstance(result, dict):
            return result
    return {}

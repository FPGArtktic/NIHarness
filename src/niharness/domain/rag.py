# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Retrieval contracts.

A chunk is addressable, because a researcher report cites chunk references and
a human must be able to follow a citation back to the thing it came from.
What "addressable" means depends on the source: code has line ranges, a
document has pages, and compacted run context has neither - it is indexed in
the run's own collection so that nothing which leaves the window is lost.

One `Chunk` carries all three, because the vector store and the retrieval tool
should not care which kind they are holding. The fields that do not apply are
absent rather than faked: a page is not a line, and pretending otherwise would
produce citations nobody can follow.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from niharness.domain.identity import Digest


class ChunkKind(StrEnum):
    """Where a chunk came from."""

    CODE = "code"
    DOC = "doc"
    RUN_CONTEXT = "run_context"


class Chunk(BaseModel):
    """One indexed unit: a function, a class, a documentation section, or compacted context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: ChunkKind
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    first_page: int | None = Field(default=None, ge=1)
    last_page: int | None = Field(default=None, ge=1)
    symbol: str = ""
    text: str
    file_digest: Digest = Field(description="Digest of the whole file; drives re-indexing.")
    provenance: str = Field(
        default="",
        description="How the text was obtained, when that is not obvious - `ocr`, say.",
    )

    @model_validator(mode="after")
    def _ranges_ordered(self) -> Self:
        """A chunk ends at or after it starts, in lines and in pages."""
        for first, last, unit in (
            (self.start_line, self.end_line, "line"),
            (self.first_page, self.last_page, "page"),
        ):
            if first is not None and last is not None and last < first:
                msg = f"chunk {self.id}: end {unit} {last} precedes start {unit} {first}"
                raise ValueError(msg)
        return self

    def citation(self) -> str:
        """Return the citation form used in researcher reports.

        Lines for code, pages for a document, the path alone for anything that
        has neither - and the OCR mark when the text was read off a rendering,
        because a reader judging a quotation should know that.
        """
        if self.start_line is not None:
            where = f"{self.path}:{self.start_line}"
            if self.end_line is not None and self.end_line != self.start_line:
                where = f"{where}-{self.end_line}"
            return f"{where} ({self.symbol})" if self.symbol else where
        if self.first_page is not None:
            pages = (
                f"p. {self.first_page}"
                if self.last_page in {None, self.first_page}
                else f"pp. {self.first_page}-{self.last_page}"
            )
            marked = f"{self.path} {pages}"
            return f"{marked} (OCR)" if self.provenance == "ocr" else marked
        return self.path


class Hit(BaseModel):
    """A retrieved chunk with its score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk: Chunk
    score: float


# ---------------------------------------------------------------- retrieval policy

MAX_HIT_CHARS = 2_000
"""How much of one chunk is rendered. A chunk this long is already a mistake."""


def collections_for(
    requested: str,
    *,
    code: str,
    docs: str,
    run: str = "",
) -> tuple[str, ...]:
    """Return the collections one query searches.

    A query that names a collection searches that one and nothing else; the
    caller asked a specific question. A query that names none searches
    everything the run has: the code index, the ingested documents, and - when
    there is one - the run's own collection, which is where compaction put the
    context that left the window.

    Searching the run's own collection by default is the reason compaction can
    drop a tool result without losing it. Making the model name a collection
    first would mean it has to know that, and it does not.

    Args:
        requested: a collection name, or empty for the default set.
        code: the code collection's name.
        docs: the ingested-documents collection's name.
        run: the run's own collection, when the run has one.

    Returns:
        Collection names, in search order, without duplicates or blanks.
    """
    wanted = (requested,) if requested else (code, docs, run)
    return tuple(dict.fromkeys(name for name in wanted if name))


def merge_hits(groups: Iterable[Sequence[Hit]], top_k: int) -> tuple[Hit, ...]:
    """Merge per-collection results into one ranking, best first.

    Scores from one embedding model over one metric are comparable across
    collections, so the merge is a sort. A chunk found in two collections is
    kept once, at its best score - the same document indexed twice should not
    occupy two of the few slots a window can afford.

    Args:
        groups: each collection's hits.
        top_k: how many to keep.

    Returns:
        At most `top_k` hits, highest score first.
    """
    best: dict[str, Hit] = {}
    for group in groups:
        for hit in group:
            kept = best.get(hit.chunk.id)
            if kept is None or hit.score > kept.score:
                best[hit.chunk.id] = hit
    ranked = sorted(best.values(), key=lambda hit: -hit.score)
    return tuple(ranked[:top_k])


def render_hits(hits: Sequence[Hit], *, searched: Sequence[str] = ()) -> str:
    """Render hits for the model: what was searched, then each citation and text.

    The citation comes before the text so that a model quoting a passage has
    the reference in front of it, and so a human reading the event log can
    follow one. An empty result says which collections were searched, because
    "nothing matched" and "nothing is indexed" look identical otherwise.

    Args:
        hits: the merged ranking.
        searched: the collections that were searched.

    Returns:
        The text the tool returns.
    """
    where = ", ".join(searched)
    if not hits:
        return f"no matches in {where}" if where else "no matches"
    lines = [f"{len(hits)} match(es) in {where}:" if where else f"{len(hits)} match(es):"]
    for position, hit in enumerate(hits, start=1):
        body = hit.chunk.text.strip()
        if len(body) > MAX_HIT_CHARS:
            body = f"{body[:MAX_HIT_CHARS]}\n... [chunk truncated]"
        lines += ["", f"[{position}] {hit.chunk.citation()}  score {hit.score:.3f}", body]
    return "\n".join(lines)

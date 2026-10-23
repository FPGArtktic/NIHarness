# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih ingest`: read documents into citable chunks.

The pipeline's retrieval is for code, and a run needs more than code: a
datasheet, a standard, an errata sheet. This reads such a document - its text
layer where it has one, OCR where it does not - and writes the chunks a
retriever indexes, each carrying the pages it came from.

The chunks are indexed into the vector store and also written to
`.nih/corpus/<digest>.jsonl`. The file is not a cache: it is what the store was
handed, so a retrieval result that looks wrong can be checked against the text
that produced it without querying anything, and a corpus can be diffed across
two runs of the reader. Indexing can be skipped with `--no-index`, which is
what to do when the store is not running and the chunking itself is the
question.
"""

from __future__ import annotations

from pathlib import Path

import typer

from niharness.adapters.pdf import PdfReader, PdfUnavailableError
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.cli.retrieval import open_retrieval
from niharness.config import HarnessConfig
from niharness.domain.documents import ChunkPolicy, OcrPolicy, chunk_pages, read_summary
from niharness.domain.identity import digest_text
from niharness.domain.rag import Chunk
from niharness.ports.vector_store import VectorStoreError

EX_UNAVAILABLE = 69
"""`sysexits.h`: a program this command needs is not installed, or is not running."""


def corpus_path(config: HarnessConfig, document: Path) -> Path:
    """Return where a document's chunks are written.

    Named by the document's path rather than its content, so re-ingesting a
    revised document replaces its chunks instead of accumulating two copies of
    a standard that was amended.
    """
    return config.state_dir / "corpus" / f"{digest_text(str(document))}.jsonl"


def cited_as(document: Path, root: Path) -> str:
    """Return the name a chunk from `document` is cited by.

    Relative to the project root when the document is inside it, because that
    is the name a reader can follow and the only one that survives being read
    on another machine - a citation naming someone's `/tmp` is a citation
    nobody can check.

    A document outside the project keeps its path. That is the honest answer:
    it has no project-relative name, and inventing one would point at a file
    that is not there.
    """
    try:
        return str(document.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(document)


async def ingest_document(
    document: Path,
    *,
    root: Path,
    languages: tuple[str, ...],
    dpi: int,
) -> tuple[tuple[Chunk, ...], str]:
    """Read one document and return its chunks and a line about how it was read.

    Raises:
        PdfUnavailableError: when the document cannot be read.
    """
    reader = PdfReader(policy=OcrPolicy(languages=languages, dpi=dpi))
    pages = await reader.read(str(document))
    body = document.read_bytes()
    chunks = chunk_pages(
        cited_as(document, root),
        pages,
        digest_text(body.decode("latin-1")),
        ChunkPolicy(),
    )
    return chunks, read_summary(pages)


def write_corpus(target: Path, chunks: tuple[Chunk, ...]) -> None:
    """Write chunks as JSONL, one per line, replacing whatever was there."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(f"{chunk.model_dump_json()}\n" for chunk in chunks),
        encoding="utf-8",
    )


async def index_chunks(
    config: HarnessConfig,
    chunks: tuple[Chunk, ...],
) -> tuple[int, int, str]:
    """Replace each document's chunks in the documents collection.

    Replace, not add. A chunk's id comes from the document's content, so a
    revised document produces new ids and the previous version would stay
    indexed - and be cited as current, which for a corpus of datasheets and
    errata is the normal case rather than an edge one. Each document's path is
    forgotten first; the path is the one identity that survives a revision.

    Returns:
        How many points were written, how many the revision displaced, and
        the collection they went to.

    Raises:
        VectorStoreError: when the store is absent or refuses them. Ingesting
            without indexing is `--no-index`; a store that was asked for and
            is not there is a failure, because the alternative is a command
            that says it ingested something nothing can find.
    """
    collection = config.vector.doc_collection
    async with open_retrieval(config) as retrieval:
        if retrieval.store is None:
            raise VectorStoreError(retrieval.why)
        removed = 0
        for path in dict.fromkeys(chunk.path for chunk in chunks):
            removed += await retrieval.store.forget(collection, path)
        written = await retrieval.store.upsert(collection, chunks)
        return written, removed, collection


def ingest(
    config: HarnessConfig,
    documents: list[Path],
    *,
    languages: str,
    dpi: int,
    index: bool,
    json_mode: bool,
) -> None:
    """Read each document, write its chunks, and index them.

    Raises:
        typer.Exit: when a document cannot be read, a program is missing, or
            indexing was asked for and the store is not there.
    """
    chosen = tuple(part for part in languages.split("+") if part) or ("eng",)
    written: list[dict[str, object]] = []
    human: list[str] = []
    collected: list[Chunk] = []
    for document in documents:
        if not document.is_file():
            fail(f"no such document: {document}", json_mode=json_mode)
            raise typer.Exit(1)
        try:
            chunks, summary = blocking(
                ingest_document(
                    document,
                    root=config.project_root,
                    languages=chosen,
                    dpi=dpi,
                ),
            )
        except PdfUnavailableError as error:
            fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
            raise typer.Exit(EX_UNAVAILABLE) from error
        target = corpus_path(config, document)
        write_corpus(target, chunks)
        collected += chunks
        written.append(
            {
                "document": str(document),
                "read": summary,
                "chunks": len(chunks),
                "corpus": str(target),
                "citations": [chunk.citation() for chunk in chunks[:3]],
            },
        )
        human.append(f"{document}: {summary}, {len(chunks)} chunk(s) -> {target}")
        human += [f"    {chunk.citation()}" for chunk in chunks[:3]]
        if len(chunks) > 3:
            human.append(f"    ... and {len(chunks) - 3} more")

    indexed: dict[str, object] = {"indexed": False}
    if not index:
        human.append("\nnot indexed (--no-index): the chunks are on disk only")
    elif collected:
        try:
            points, removed, collection = blocking(index_chunks(config, tuple(collected)))
        except VectorStoreError as error:
            fail(
                f"the chunks are written, but indexing them failed: {error}",
                json_mode=json_mode,
                code=EX_UNAVAILABLE,
            )
            raise typer.Exit(EX_UNAVAILABLE) from error
        indexed = {
            "indexed": True,
            "points": points,
            "replaced": removed,
            "collection": collection,
        }
        line = f"\nindexed {points} chunk(s) into {collection}; rag_query can find them"
        if removed:
            line = f"{line}\n  replaced {removed} chunk(s) from an earlier revision"
        human.append(line)
    emit(
        {"ok": True, "documents": written, "languages": list(chosen), **indexed},
        json_mode=json_mode,
        human="\n".join(human),
    )

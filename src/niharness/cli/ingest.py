# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih ingest`: read documents into citable chunks.

The pipeline's retrieval is for code, and a run needs more than code: a
datasheet, a standard, an errata sheet. This reads such a document - its text
layer where it has one, OCR where it does not - and writes the chunks a
retriever indexes, each carrying the pages it came from.

The chunks land in `.nih/corpus/<digest>.jsonl` because the vector store
adapter does not exist yet (see `docs/roadmap.md`). That is a deliberate
staging post rather than a placeholder: the file is exactly what the store will
be handed, so the half that is built can be inspected, diffed and re-run
without one.
"""

from __future__ import annotations

from pathlib import Path

import typer

from niharness.adapters.pdf import PdfReader, PdfUnavailableError
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.config import HarnessConfig
from niharness.domain.documents import ChunkPolicy, OcrPolicy, chunk_pages, read_summary
from niharness.domain.identity import digest_text
from niharness.domain.rag import Chunk

EX_UNAVAILABLE = 69
"""`sysexits.h`: a program this command needs is not installed."""


def corpus_path(config: HarnessConfig, document: Path) -> Path:
    """Return where a document's chunks are written.

    Named by the document's path rather than its content, so re-ingesting a
    revised document replaces its chunks instead of accumulating two copies of
    a standard that was amended.
    """
    return config.state_dir / "corpus" / f"{digest_text(str(document))}.jsonl"


async def ingest_document(
    document: Path,
    *,
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
        str(document),
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


def ingest(
    config: HarnessConfig,
    documents: list[Path],
    *,
    languages: str,
    dpi: int,
    json_mode: bool,
) -> None:
    """Read each document and write its chunks.

    Raises:
        typer.Exit: when a document cannot be read, or a program is missing.
    """
    chosen = tuple(part for part in languages.split("+") if part) or ("eng",)
    written: list[dict[str, object]] = []
    human: list[str] = []
    for document in documents:
        if not document.is_file():
            fail(f"no such document: {document}", json_mode=json_mode)
            raise typer.Exit(1)
        try:
            chunks, summary = blocking(
                ingest_document(document, languages=chosen, dpi=dpi),
            )
        except PdfUnavailableError as error:
            fail(str(error), json_mode=json_mode, code=EX_UNAVAILABLE)
            raise typer.Exit(EX_UNAVAILABLE) from error
        target = corpus_path(config, document)
        write_corpus(target, chunks)
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
    emit(
        {"ok": True, "documents": written, "languages": list(chosen)},
        json_mode=json_mode,
        human="\n".join(human)
        + "\n\nthe vector store does not exist yet, so nothing is indexed: "
        "see docs/roadmap.md",
    )

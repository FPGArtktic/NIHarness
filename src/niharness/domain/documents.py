# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Documents, pages and the chunks a retriever can cite.

A codebase is not the only thing a run needs to read. A datasheet, a standard,
a vendor's errata sheet - the answer is in a PDF, and half of those PDFs are
scans with no text in them at all.

This module is the policy and none of the work: which pages exist, whether a
page's text layer is real or an artefact of a scanner, and how pages become
chunks that can be cited. Rendering a page and running an OCR engine belong to
an adapter, because they are processes.

Two decisions shape it:

* **a chunk cites a page.** A retrieved passage that cannot be checked is worth
  very little to a reviewer and nothing to a gate, so a chunk carries the pages
  it came from and renders them the way a person would write them: `p. 12`, or
  `pp. 12-13`.
* **a page is never silently dropped.** A page with no text is recorded as a
  page with no text. The alternative - omitting it - makes a document look
  complete when it is not, and that is the kind of quiet lie this project
  refuses elsewhere too.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from niharness.domain.identity import Digest
from niharness.domain.rag import Chunk, ChunkKind


class TextSource(StrEnum):
    """Where a page's text came from."""

    TEXT_LAYER = "text_layer"
    """Extracted from the document itself: exact, free, and often absent."""

    OCR = "ocr"
    """Read off a rendering of the page: approximate, and the only option for a scan."""

    EMPTY = "empty"
    """Neither worked. A blank page and a failed read are both recorded, never dropped."""


class PageText(BaseModel):
    """One page's text, and how it was obtained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int = Field(ge=1, description="Page number as a reader would cite it.")
    text: str = ""
    source: TextSource = TextSource.TEXT_LAYER

    @property
    def characters(self) -> int:
        """How much text this page has, after stripping."""
        return len(self.text.strip())


class OcrPolicy(BaseModel):
    """When a page's text layer is not worth trusting.

    A scanned page usually has *some* text layer: a few stray characters from a
    watermark, a header added by the scanner, or the ligature soup a bad
    extractor produces. The two thresholds separate that from real text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_characters: int = Field(
        default=80,
        ge=0,
        description="Below this many characters a page is treated as unread.",
    )
    min_alphanumeric_ratio: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        description="Below this share of letters and digits the text layer is noise.",
    )
    languages: tuple[str, ...] = Field(
        default=("eng",),
        min_length=1,
        description="OCR languages, in the engine's own notation.",
    )
    dpi: int = Field(default=300, ge=72, le=1200)


def alphanumeric_ratio(text: str) -> float:
    """Return the share of `text` that is letters or digits, ignoring whitespace."""
    visible = [character for character in text if not character.isspace()]
    if not visible:
        return 0.0
    return sum(character.isalnum() for character in visible) / len(visible)


def needs_ocr(text: str, policy: OcrPolicy) -> bool:
    """Return whether this page has to be read off a rendering instead.

    Args:
        text: whatever the document's own text layer gave for the page.
        policy: the thresholds in force.

    Returns:
        True when the text layer is too short or too noisy to be real text.
    """
    stripped = text.strip()
    if len(stripped) < policy.min_characters:
        return True
    return alphanumeric_ratio(stripped) < policy.min_alphanumeric_ratio


class ChunkPolicy(BaseModel):
    """How pages are packed into chunks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_characters: int = Field(default=2_000, ge=200)
    """A chunk this size embeds well and still carries its context."""

    min_characters: int = Field(default=200, ge=0)
    """A chunk smaller than this takes the next page with it, when there is one."""


def chunk_pages(
    document: str,
    pages: Iterable[PageText],
    file_digest: Digest,
    policy: ChunkPolicy | None = None,
) -> tuple[Chunk, ...]:
    """Pack pages into citable chunks.

    A page boundary is a natural boundary, so chunks follow pages: small pages
    are merged with their neighbours until the chunk is worth embedding, and a
    page too large for one chunk is split on blank lines - a paragraph break is
    the next best boundary after a page break.

    Args:
        document: the document's path, as a citation names it.
        pages: the pages, in order.
        file_digest: digest of the whole document, which drives re-indexing.
        policy: sizes; the shipped defaults when omitted.

    Returns:
        Chunks in reading order, each carrying the pages it came from and
        whether any of that text was read by OCR.
    """
    rules = policy or ChunkPolicy()
    chunks: list[Chunk] = []
    pending: list[PageText] = []

    def flush() -> None:
        """Turn the pending pages into one chunk."""
        if not pending:
            return
        text = "\n\n".join(page.text.strip() for page in pending if page.text.strip())
        sources = {page.source for page in pending}
        chunks.append(
            Chunk(
                id=f"{file_digest}:{len(chunks)}",
                kind=ChunkKind.DOC,
                path=document,
                first_page=pending[0].number,
                last_page=pending[-1].number,
                text=text,
                file_digest=file_digest,
                provenance=TextSource.OCR.value if TextSource.OCR in sources else "",
            ),
        )
        pending.clear()

    for page in pages:
        for part in _split_page(page, rules.max_characters):
            pending.append(part)
            if sum(item.characters for item in pending) >= rules.min_characters:
                flush()
    flush()
    return tuple(chunks)


def _split_page(page: PageText, limit: int) -> tuple[PageText, ...]:
    """Return the page, or the pieces of it that fit a chunk."""
    if page.characters <= limit:
        return (page,)
    pieces: list[PageText] = []
    current: list[str] = []
    length = 0
    for paragraph in page.text.split("\n\n"):
        if current and length + len(paragraph) > limit:
            pieces.append(page.model_copy(update={"text": "\n\n".join(current)}))
            current, length = [], 0
        current.append(paragraph)
        length += len(paragraph) + 2
    if current:
        pieces.append(page.model_copy(update={"text": "\n\n".join(current)}))
    return tuple(pieces)


def read_summary(pages: Sequence[PageText]) -> str:
    """Return one line about how a document was read.

    The share that needed OCR is the number worth knowing: it is the difference
    between a document that was read and one that was guessed at.
    """
    if not pages:
        return "no pages"
    counts = dict.fromkeys(TextSource, 0)
    for page in pages:
        counts[page.source] += 1
    parts = [f"{count} {source.value}" for source, count in counts.items() if count]
    return f"{len(pages)} page(s): {', '.join(parts)}"

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

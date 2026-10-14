# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Retrieval contracts.

Chunks are addressable by file and line range, because a researcher report
cites chunk references and a human must be able to follow a citation back to
the code. Compacted run context is indexed as chunks too, in the run's own
collection, so nothing that leaves the window is actually lost.
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
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str = ""
    text: str
    file_digest: Digest = Field(description="Digest of the whole file; drives re-indexing.")

    @model_validator(mode="after")
    def _lines_ordered(self) -> Self:
        """A chunk ends at or after its start."""
        if self.end_line < self.start_line:
            msg = (
                f"chunk {self.id}: end_line {self.end_line} precedes "
                f"start_line {self.start_line}"
            )
            raise ValueError(msg)
        return self

    def citation(self) -> str:
        """Return the citation form used in researcher reports."""
        where = f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{where} ({self.symbol})" if self.symbol else where


class Hit(BaseModel):
    """A retrieved chunk with its score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk: Chunk
    score: float

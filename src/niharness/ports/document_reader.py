# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The document port.

One question: what does this document say, page by page? How that is answered
- a text layer, an OCR engine, a vision model - is the adapter's business, and
each page records which of them produced it (`PageText.source`), because a
passage read off a rendering deserves to be recognisable as one.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from niharness.domain.documents import PageText


@runtime_checkable
class DocumentReader(Protocol):
    """Reads a document into pages of text."""

    async def read(self, path: str) -> tuple[PageText, ...]:
        """Return every page of `path`, in order.

        A page that cannot be read is returned empty rather than omitted: a
        document that looks complete and is not is worse than a visible gap.
        """
        ...

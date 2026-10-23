# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The embedding port.

Separate from :class:`~niharness.ports.model_backend.ModelBackend` because an
embedding model is not a chat model: it has no window to fill, no tools, no
reasoning, and its one answer is a vector. Keeping it apart means the vector
store can be handed an embedder without being handed the ability to generate.

`dimensions` is declared rather than discovered, because a collection has to be
created before the first vector exists. An implementation is expected to check
what the model actually returns against what it declared - a collection built
at the wrong width accepts nothing, and the error it produces then names
neither the model nor the number.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

Vector = tuple[float, ...]
"""One embedding."""


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors of a fixed width."""

    @property
    def dimensions(self) -> int:
        """The width of every vector this embedder returns."""
        ...

    async def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """Embed each text, in the order given.

        Returns:
            One vector per input, same length, same order.
        """
        ...

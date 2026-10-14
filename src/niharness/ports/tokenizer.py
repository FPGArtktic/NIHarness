# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tokenizer port.

Token counts come from the backend's own tokenizer, never from a character
heuristic: compaction decisions and budget enforcement both depend on the
number being the one the model will actually see.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from niharness.domain.messages import Message


@runtime_checkable
class Tokenizer(Protocol):
    """Counts tokens the way the serving backend counts them."""

    async def count_text(self, text: str) -> int:
        """Return the token count of `text`."""
        ...

    async def count_messages(self, messages: Sequence[Message]) -> int:
        """Return the token count of a whole message list, template included."""
        ...

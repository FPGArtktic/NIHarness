# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The OpenAI-compatible inference adapter: chat, tokens and embeddings."""

from niharness.adapters.local_http import AirGapViolationError
from niharness.adapters.openai_compat.client import OpenAiCompatBackend
from niharness.adapters.openai_compat.embeddings import (
    EmbeddingWidthError,
    OpenAiCompatEmbedder,
)

__all__ = [
    "AirGapViolationError",
    "EmbeddingWidthError",
    "OpenAiCompatBackend",
    "OpenAiCompatEmbedder",
]

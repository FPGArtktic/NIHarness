# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The OpenAI-compatible inference adapter."""

from niharness.adapters.openai_compat.client import AirGapViolationError, OpenAiCompatBackend

__all__ = ["AirGapViolationError", "OpenAiCompatBackend"]

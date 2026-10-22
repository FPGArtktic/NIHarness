# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading PDFs: a text layer when there is one, OCR when there is not."""

from niharness.adapters.pdf.reader import PdfReader, PdfUnavailableError

__all__ = ["PdfReader", "PdfUnavailableError"]

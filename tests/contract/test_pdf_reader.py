# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading PDFs, against real PDFs and a real OCR engine.

The fixtures are generated rather than committed: a typeset document from
PostScript, and the same document rasterised into an image-only PDF, which is
what a scanner produces and what half the datasheets in the world are. Both
need the programs the sandbox image installs, so the tests skip where they are
absent instead of pretending to pass.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from niharness.adapters.pdf import PdfReader, PdfUnavailableError
from niharness.domain.documents import OcrPolicy, TextSource

POSTSCRIPT = """%!PS
/Helvetica findfont 14 scalefont setfont
72 700 moveto (The receiver clock is recovered from the data stream by a) show
72 680 moveto (digital phase-locked loop. The loop bandwidth is programmable) show
72 660 moveto (between 1 and 16 kilohertz, and the default is 4.) show
showpage
72 700 moveto (Appendix A: pinout. This page is deliberately short.) show
showpage
"""

needs_poppler = pytest.mark.skipif(
    shutil.which("pdftotext") is None or shutil.which("pdfinfo") is None,
    reason="poppler is not installed",
)
needs_ghostscript = pytest.mark.skipif(
    shutil.which("ps2pdf") is None or shutil.which("gs") is None,
    reason="ghostscript is not installed",
)
needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None or shutil.which("pdftoppm") is None,
    reason="tesseract or pdftoppm is not installed",
)


@pytest.fixture
def typeset(tmp_path: Path) -> Path:
    """A two-page PDF with a real text layer."""
    source = tmp_path / "doc.ps"
    source.write_text(POSTSCRIPT, encoding="utf-8")
    target = tmp_path / "manual.pdf"
    subprocess.run(["ps2pdf", str(source), str(target)], check=True, capture_output=True)
    return target


@pytest.fixture
def scanned(typeset: Path) -> Path:
    """The same document with its text layer thrown away, as a scanner leaves it."""
    target = typeset.with_name("scan.pdf")
    subprocess.run(
        ["gs", "-q", "-sDEVICE=pdfimage24", "-r150", f"-o{target}", str(typeset)],
        check=True,
        capture_output=True,
    )
    return target


@needs_poppler
@needs_ghostscript
async def test_a_typeset_document_is_read_from_its_text_layer(typeset: Path) -> None:
    pages = await PdfReader().read(str(typeset))
    assert len(pages) == 2
    assert [page.number for page in pages] == [1, 2]
    assert pages[0].source is TextSource.TEXT_LAYER
    assert "phase-locked loop" in pages[0].text


@needs_poppler
@needs_ghostscript
@needs_tesseract
async def test_a_scan_is_read_by_ocr(scanned: Path) -> None:
    # pdftotext returns nothing but form feeds for this file, so every word
    # here came off a rendering.
    pages = await PdfReader().read(str(scanned))
    assert len(pages) == 2
    assert all(page.source is TextSource.OCR for page in pages)
    assert "receiver clock" in pages[0].text
    assert "Appendix A" in pages[1].text


@needs_poppler
@needs_ghostscript
@needs_tesseract
async def test_ocr_has_to_beat_the_text_layer_to_replace_it(typeset: Path) -> None:
    # Page two is a single short line, which trips the OCR threshold. The
    # engine then returns no more than the document already said, and the
    # exact text is the better answer.
    pages = await PdfReader().read(str(typeset))
    assert pages[1].source is TextSource.TEXT_LAYER
    assert "deliberately short" in pages[1].text


@needs_poppler
@needs_ghostscript
async def test_a_page_count_is_read_before_any_page(typeset: Path) -> None:
    assert await PdfReader().page_count(str(typeset)) == 2


@needs_poppler
async def test_something_that_is_not_a_pdf_is_refused(tmp_path: Path) -> None:
    impostor = tmp_path / "notes.pdf"
    impostor.write_text("This is not a PDF at all.\n", encoding="utf-8")
    with pytest.raises(PdfUnavailableError, match="not a readable PDF"):
        await PdfReader().read(str(impostor))


async def test_a_missing_program_is_named(tmp_path: Path) -> None:
    # The sandbox image installs these. A host that does not have them should
    # hear which one, not a traceback from a subprocess.
    empty = tmp_path / "x.pdf"
    empty.write_bytes(b"%PDF-1.4\n")
    reader = PdfReader(which=lambda _program: None)
    with pytest.raises(PdfUnavailableError, match="pdfinfo is not installed"):
        await reader.read(str(empty))


@needs_poppler
@needs_ghostscript
async def test_the_ocr_languages_are_the_project_s_to_choose(typeset: Path) -> None:
    # Polish and English together is the setting this project ships for a
    # bilingual corpus; the point of the test is that it reaches the engine.
    reader = PdfReader(policy=OcrPolicy(languages=("pol", "eng"), dpi=150))
    pages = await reader.read(str(typeset))
    assert len(pages) == 2

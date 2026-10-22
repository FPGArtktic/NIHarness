# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pages out of a PDF, with poppler and an OCR engine.

Two programs, no new Python dependency, and both already belong in the sandbox
image: `pdftotext` for a document that has a text layer, `pdftoppm` plus
`tesseract` for the half of the world's datasheets that are photographs of
paper. The harness shells out to a toolchain everywhere else - the gate does
nothing but that - so a document is read the same way.

The decision of *which* to use per page is not here. It is
`niharness.domain.documents.needs_ocr`, which is pure and therefore testable
without a PDF, and this adapter asks it page by page. OCR is per page rather
than per document on purpose: a scanned appendix at the back of a typeset
standard is normal, and rendering 400 clean pages to read 3 of them is not.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

import structlog

from niharness.domain.documents import OcrPolicy, PageText, TextSource, needs_ocr
from niharness.ports.sandbox import ProcessResult

PAGE_COUNT = re.compile(r"^Pages:\s+(\d+)$", re.MULTILINE)
"""How `pdfinfo` reports the page count."""

PDFINFO = "pdfinfo"
PDFTOTEXT = "pdftotext"
PDFTOPPM = "pdftoppm"
TESSERACT = "tesseract"

MAX_PAGES = 2_000
"""A document longer than this is refused rather than read for an hour."""

_log = structlog.get_logger(__name__)


class PdfUnavailableError(RuntimeError):
    """A program this reader needs is not installed, or the document is unusable."""


class PdfReader:
    """A :class:`niharness.ports.document_reader.DocumentReader` over poppler."""

    def __init__(
        self,
        *,
        policy: OcrPolicy | None = None,
        timeout_seconds: int = 300,
        which: object = shutil.which,
    ) -> None:
        """Describe how documents are read.

        Args:
            policy: when a page needs OCR, in which languages, at what
                resolution. The shipped defaults when omitted.
            timeout_seconds: how long any single program may take.
            which: executable lookup, so a test can pretend a tool is absent.
        """
        self._policy = policy or OcrPolicy()
        self._timeout = timeout_seconds
        self._which = which if callable(which) else shutil.which

    async def read(self, path: str) -> tuple[PageText, ...]:
        """Return the document's pages, reading each the cheapest way that works.

        Raises:
            PdfUnavailableError: when poppler is missing, the file is not a
                readable PDF, or it is longer than `MAX_PAGES`.
        """
        document = Path(path)
        count = await self.page_count(path)
        pages: list[PageText] = []
        with tempfile.TemporaryDirectory(prefix="nih-pdf-") as scratch:
            for number in range(1, count + 1):
                pages.append(await self._page(document, number, Path(scratch)))
        ocr = sum(page.source is TextSource.OCR for page in pages)
        _log.info("document.read", path=path, pages=len(pages), ocr=ocr)
        return tuple(pages)

    async def page_count(self, path: str) -> int:
        """Return how many pages the document has.

        Raises:
            PdfUnavailableError: when `pdfinfo` is absent, the file is not a
                readable PDF, or the document is too long to read.
        """
        self._require(PDFINFO)
        result = await self._run((PDFINFO, path))
        if not result.ok:
            raise PdfUnavailableError(f"{path} is not a readable PDF: {result.output.strip()}")
        found = PAGE_COUNT.search(result.stdout)
        if found is None:
            raise PdfUnavailableError(f"{path}: pdfinfo reported no page count")
        count = int(found.group(1))
        if count > MAX_PAGES:
            raise PdfUnavailableError(f"{path} has {count} pages; the limit is {MAX_PAGES}")
        return count

    async def _page(self, document: Path, number: int, scratch: Path) -> PageText:
        """Return one page, from the text layer or from OCR."""
        extracted = await self._text_layer(document, number)
        if not needs_ocr(extracted, self._policy):
            return PageText(number=number, text=extracted, source=TextSource.TEXT_LAYER)
        recognised = await self._ocr(document, number, scratch)
        # OCR has to beat the text layer to replace it. A legitimately short
        # page - an appendix heading, a figure caption - trips the threshold
        # and gets rendered, and when the engine comes back with no more than
        # the document already said, the exact text is the better answer.
        if len(recognised.strip()) > len(extracted.strip()):
            return PageText(number=number, text=recognised, source=TextSource.OCR)
        # Neither worked. The page is still a page: see the port's contract.
        return PageText(
            number=number,
            text=extracted,
            source=TextSource.TEXT_LAYER if extracted.strip() else TextSource.EMPTY,
        )

    async def _text_layer(self, document: Path, number: int) -> str:
        """Return what the document itself says this page contains."""
        self._require(PDFTOTEXT)
        result = await self._run(
            (PDFTOTEXT, "-layout", "-f", str(number), "-l", str(number), str(document), "-"),
        )
        return result.stdout if result.ok else ""

    async def _ocr(self, document: Path, number: int, scratch: Path) -> str:
        """Render one page and read it with the OCR engine."""
        if self._which(PDFTOPPM) is None or self._which(TESSERACT) is None:
            _log.info("document.ocr_unavailable", page=number)
            return ""
        prefix = scratch / f"page-{number}"
        rendered = await self._run(
            (
                PDFTOPPM,
                "-r",
                str(self._policy.dpi),
                "-png",
                "-f",
                str(number),
                "-l",
                str(number),
                str(document),
                str(prefix),
            ),
        )
        if not rendered.ok:
            _log.warning("document.render_failed", page=number, error=rendered.output.strip())
            return ""
        images = sorted(scratch.glob(f"page-{number}*.png"))
        if not images:
            return ""
        result = await self._run(
            (TESSERACT, str(images[0]), "stdout", "-l", "+".join(self._policy.languages)),
        )
        if not result.ok:
            _log.warning("document.ocr_failed", page=number, error=result.output.strip()[:200])
            return ""
        return result.stdout

    def _require(self, program: str) -> None:
        """Refuse clearly when a program is not installed.

        Raises:
            PdfUnavailableError: when `program` is not on the path.
        """
        if self._which(program) is None:
            raise PdfUnavailableError(
                f"{program} is not installed; it belongs in the sandbox image",
            )

    async def _run(self, argv: tuple[str, ...]) -> ProcessResult:
        """Run one program and collect what it said."""
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise PdfUnavailableError(f"{argv[0]} did not start: {error}") from error
        try:
            out, err = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ProcessResult(
                argv=argv,
                exit_code=124,
                stdout="",
                stderr=f"{argv[0]} took longer than {self._timeout}s",
                duration_seconds=float(self._timeout),
            )
        return ProcessResult(
            argv=argv,
            exit_code=process.returncode or 0,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            duration_seconds=asyncio.get_running_loop().time() - started,
        )

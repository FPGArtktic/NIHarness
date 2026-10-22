# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Documents: when a page needs OCR, and how pages become citable chunks."""

from hypothesis import given
from hypothesis import strategies as st

from niharness.domain.documents import (
    ChunkPolicy,
    OcrPolicy,
    PageText,
    TextSource,
    alphanumeric_ratio,
    chunk_pages,
    needs_ocr,
    read_summary,
)
from niharness.domain.identity import Digest
from niharness.domain.rag import ChunkKind

DIGEST = Digest("0123456789abcdef")
REAL_TEXT = (
    "The receiver clock is recovered from the data stream by a digital "
    "phase-locked loop. The loop bandwidth is programmable between 1 and 16 "
    "kilohertz, and the default is 4."
)


def test_a_page_with_real_text_is_not_sent_to_ocr() -> None:
    assert not needs_ocr(REAL_TEXT, OcrPolicy())


def test_an_empty_page_needs_ocr() -> None:
    # A scan's text layer is usually empty, and a blank page looks the same.
    # Both get the same answer here, and the adapter finds out which it was.
    assert needs_ocr("", OcrPolicy())
    assert needs_ocr("   \n\n  ", OcrPolicy())


def test_a_page_with_a_scanner_header_only_needs_ocr() -> None:
    assert needs_ocr("Scanned by DocuScan 4000", OcrPolicy())


def test_a_page_of_extraction_noise_needs_ocr() -> None:
    # What a bad extractor produces from a scan: punctuation and ligature soup
    # in roughly the right quantity. The character count alone would accept it.
    noise = "|-|.. ~~ ,,, ``` ||| ... --- ;;; ^^^ *** ((( ))) [[[ ]]] {{{ }}} +++ === /// " * 2
    assert len(noise) > OcrPolicy().min_characters
    assert needs_ocr(noise, OcrPolicy())


def test_the_thresholds_are_the_project_s_to_set() -> None:
    lenient = OcrPolicy(min_characters=5, min_alphanumeric_ratio=0.1)
    assert not needs_ocr("Scanned by DocuScan 4000", lenient)


def test_the_ratio_ignores_whitespace() -> None:
    assert alphanumeric_ratio("ab\n\ncd") == 1.0
    assert alphanumeric_ratio("") == 0.0
    assert alphanumeric_ratio("....") == 0.0


# ------------------------------------------------------------------- chunking


def pages(*texts: str, source: TextSource = TextSource.TEXT_LAYER) -> list[PageText]:
    return [
        PageText(number=index, text=text, source=source)
        for index, text in enumerate(texts, start=1)
    ]


def test_a_chunk_cites_the_pages_it_came_from() -> None:
    chunks = chunk_pages("manual.pdf", pages(REAL_TEXT), DIGEST)
    assert len(chunks) == 1
    assert chunks[0].citation() == "manual.pdf p. 1"
    assert chunks[0].kind is ChunkKind.DOC
    assert chunks[0].first_page == 1


def test_small_pages_are_merged_and_the_citation_spans_them() -> None:
    chunks = chunk_pages(
        "manual.pdf",
        pages("one", "two", "three", "four"),
        DIGEST,
        ChunkPolicy(min_characters=12, max_characters=2_000),
    )
    assert len(chunks) == 1
    assert chunks[0].citation() == "manual.pdf pp. 1-4"


def test_a_page_too_large_for_a_chunk_is_split_on_paragraphs() -> None:
    body = "\n\n".join(["sentence " * 20] * 6)
    chunks = chunk_pages(
        "manual.pdf",
        pages(body),
        DIGEST,
        ChunkPolicy(max_characters=400, min_characters=100),
    )
    assert len(chunks) > 1
    assert all(chunk.first_page == 1 for chunk in chunks), "all still page 1"
    assert [chunk.ordinal if hasattr(chunk, "ordinal") else i for i, chunk in enumerate(chunks)]


def test_an_ocr_chunk_says_so_in_its_citation() -> None:
    chunks = chunk_pages("scan.pdf", pages(REAL_TEXT, source=TextSource.OCR), DIGEST)
    assert chunks[0].provenance == "ocr"
    assert chunks[0].citation() == "scan.pdf p. 1 (OCR)"
    # A reader judging a quotation should know it was read off a rendering.


def test_chunk_ids_are_stable_and_distinct() -> None:
    chunks = chunk_pages(
        "manual.pdf",
        pages(REAL_TEXT, REAL_TEXT, REAL_TEXT),
        DIGEST,
        ChunkPolicy(min_characters=10),
    )
    identifiers = [chunk.id for chunk in chunks]
    assert len(set(identifiers)) == len(identifiers)
    again = chunk_pages(
        "manual.pdf",
        pages(REAL_TEXT, REAL_TEXT, REAL_TEXT),
        DIGEST,
        ChunkPolicy(min_characters=10),
    )
    assert [chunk.id for chunk in again] == identifiers


@given(st.lists(st.text(min_size=0, max_size=300), min_size=0, max_size=12))
def test_no_page_text_is_lost(texts: list[str]) -> None:
    # A document that looks complete and is not is the failure this guards.
    chunked = chunk_pages("manual.pdf", pages(*texts), DIGEST, ChunkPolicy(min_characters=50))
    joined = "\n\n".join(chunk.text for chunk in chunked)
    for text in texts:
        stripped = text.strip()
        if stripped:
            assert stripped in joined


def test_the_read_summary_says_how_a_document_was_read() -> None:
    mixed = [
        PageText(number=1, text=REAL_TEXT, source=TextSource.TEXT_LAYER),
        PageText(number=2, text=REAL_TEXT, source=TextSource.OCR),
        PageText(number=3, text="", source=TextSource.EMPTY),
    ]
    summary = read_summary(mixed)
    assert "3 page(s)" in summary
    assert "1 ocr" in summary
    assert "1 empty" in summary
    assert read_summary([]) == "no pages"

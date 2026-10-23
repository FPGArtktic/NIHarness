<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Documents

A run needs more than code. The answer to "what does this register do" is in a
datasheet, and half the datasheets in the world are photographs of paper.

```bash
uv run nih ingest manual.pdf --ocr-languages pol+eng
```

```text
manual.pdf: 2 page(s): 2 ocr, 1 chunk(s) -> .nih/corpus/d9cea719c2cf54cf.jsonl
    manual.pdf pp. 1-2 (OCR)

indexed 1 chunk(s) into docs; rag_query can find them
```

## Two tiers, like the gate

Reading a page cheaply and reading it well are different operations, so the
reader tries them in that order - the same shape as the gate's quick and full
tiers:

1. **the text layer**, with `pdftotext`. Exact, free, and absent from every
   scan.
2. **OCR**, with `pdftoppm` and `tesseract`, when the text layer is not real
   text.

The decision between them is pure policy, in `niharness.domain.documents`, so
it is testable without a PDF:

| Signal | Default | Why |
|---|---|---|
| `min_characters` | 80 | a scanner's header is not a page of text |
| `min_alphanumeric_ratio` | 0.45 | a bad extractor returns punctuation soup in the right quantity, and a character count alone accepts it |
| `languages` | `eng` | tesseract's own notation; `pol+eng` for a bilingual corpus |
| `dpi` | 300 | what the page is rendered at before OCR |

Two details that are easy to get wrong:

- **OCR has to beat the text layer to replace it.** A legitimately short page -
  an appendix heading, a figure caption - trips the threshold and gets
  rendered. When the engine returns no more than the document already said,
  the exact text wins.
- **per page, not per document.** A scanned appendix at the back of a typeset
  standard is normal. Rendering four hundred clean pages to read three of them
  is not.

## A chunk cites a page

Retrieval is worth very little if a passage cannot be checked, so a chunk
carries the pages it came from and renders them the way a person would write
them - `manual.pdf p. 12`, `manual.pdf pp. 12-13` - and marks the ones that
were read off a rendering:

```text
manual.pdf pp. 1-2 (OCR)
```

There is **one** `Chunk` type for code and for documents
(`niharness.domain.rag`), because the vector store and the retrieval tool
should not care which they are holding. The fields that do not apply are
absent rather than faked: a page is not a line, and pretending otherwise
produces citations nobody can follow.

Pages become chunks on page boundaries, which are natural boundaries. A small
page takes the next one with it until the chunk is worth embedding; a page too
large for one chunk is split on blank lines, a paragraph break being the next
best boundary after a page break. A page with no text still contributes its
page number, so a gap in a document is visible rather than invented away - and
a hypothesis test asserts that no page's text is lost.

## Indexed, and also written down

The chunks go into the vector store's document collection, and the same chunks
are written to `.nih/corpus/<digest>.jsonl`, one per line.

Re-ingesting a document **replaces** what it indexed before:

```text
datasheet.pdf: 1 page(s): 1 text_layer, 1 chunk(s) -> .nih/corpus/b806862e.jsonl
    datasheet.pdf p. 1

indexed 1 chunk(s) into docs; rag_query can find them
  replaced 2 chunk(s) from an earlier revision
```

That is not free, and the reason is worth knowing. A chunk's id comes from the
document's content, so a revised document produces *new* ids - its predecessor's
chunks would stay indexed and be cited as current. For a corpus of datasheets
and errata that is the normal case, not an edge one, so each document's path is
forgotten before the new chunks are written. The path is the one identity that
survives a revision.

The file is not a cache. It is what the store was handed, so a retrieval result
that looks wrong can be checked against the text that produced it without
querying anything, and a corpus can be diffed across two runs of the reader -
after a tesseract upgrade, say. `--no-index` writes the file and skips the
store, which is what to use when the chunking itself is the question.

A document read but **not** indexed is a failure, not a success with a caveat:

```console
$ uv run nih ingest manual.pdf
manual.pdf: 2 page(s): 2 ocr, 1 chunk(s) -> .nih/corpus/d9cea719c2cf54cf.jsonl
the chunks are written, but indexing them failed: no vector store at
http://127.0.0.1:6333: All connection attempts failed
$ echo $?
69
```

The chunks survive, so nothing has to be read twice. What would not survive is
an operator believing `rag_query` can find a document that nothing indexed.

From there it is [retrieval](retrieval.md): `rag_query` searches the document
collection along with the code index and the run's own context, and answers
with the citations above.

## What is not built yet

The programs belong in the sandbox image, which is where a run's tools live.
`nih ingest` runs on the host for now and says which program is missing when
one is - "pdfinfo is not installed; it belongs in the sandbox image" - rather
than failing with a traceback from a subprocess.

A **vision model** reading a page it cannot OCR - a schematic, a timing
diagram, a table whose structure is the information - is on
[the roadmap](../roadmap.md). The policy in `domain/documents.py` is already
a two-tier choice per page, so a third tier fits where the second one did.

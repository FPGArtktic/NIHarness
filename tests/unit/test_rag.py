# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Retrieval policy: what is searched, how results merge, and how they read."""

import pytest

from niharness.domain.rag import (
    MAX_HIT_CHARS,
    Chunk,
    ChunkKind,
    Hit,
    collections_for,
    merge_hits,
    render_hits,
)

DIGEST = "c" * 64


def chunk(identifier: str, text: str = "body", **kwargs: object) -> Chunk:
    fields: dict[str, object] = {
        "id": identifier,
        "kind": ChunkKind.CODE,
        "path": "src/a.py",
        "text": text,
        "file_digest": DIGEST,
    }
    fields.update(kwargs)
    return Chunk.model_validate(fields)


def test_a_query_without_a_collection_searches_everything_the_run_can_see() -> None:
    """Including the run's own collection, which is where compaction put things.

    That default is the reason a tool result can leave the window without
    being lost. Making the model name the collection first would mean it has
    to know that, and it does not.
    """
    assert collections_for("", code="code", docs="docs", run="run-7") == (
        "code",
        "docs",
        "run-7",
    )


def test_a_query_that_names_a_collection_searches_only_that_one() -> None:
    assert collections_for("docs", code="code", docs="docs", run="run-7") == ("docs",)


def test_a_run_without_its_own_collection_contributes_nothing_to_the_set() -> None:
    assert collections_for("", code="code", docs="docs") == ("code", "docs")


def test_collections_configured_to_the_same_name_are_searched_once() -> None:
    assert collections_for("", code="all", docs="all", run="all") == ("all",)


def test_hits_merge_into_one_ranking_across_collections() -> None:
    first = (Hit(chunk=chunk("a"), score=0.4), Hit(chunk=chunk("b"), score=0.9))
    second = (Hit(chunk=chunk("c"), score=0.7),)
    assert [hit.chunk.id for hit in merge_hits([first, second], 3)] == ["b", "c", "a"]


def test_a_chunk_found_in_two_collections_is_kept_once_at_its_best_score() -> None:
    """The same document indexed twice should not occupy two of very few slots."""
    merged = merge_hits(
        [(Hit(chunk=chunk("a"), score=0.3),), (Hit(chunk=chunk("a"), score=0.8),)], 5
    )
    assert len(merged) == 1
    assert merged[0].score == pytest.approx(0.8)


def test_the_merge_keeps_no_more_than_was_asked_for() -> None:
    hits = tuple(Hit(chunk=chunk(str(index)), score=index / 10) for index in range(10))
    assert len(merge_hits([hits], 4)) == 4


def test_an_empty_result_says_where_it_looked() -> None:
    """Nothing matched and nothing indexed look identical without this."""
    assert render_hits((), searched=("code", "docs")) == "no matches in code, docs"


def test_a_rendered_hit_leads_with_its_citation() -> None:
    hit = Hit(
        chunk=chunk("a", "def one(): ...", start_line=10, end_line=12, symbol="one"),
        score=0.6789,
    )
    rendered = render_hits((hit,), searched=("code",))
    assert rendered.splitlines()[0] == "1 match(es) in code:"
    assert "[1] src/a.py:10-12 (one)  score 0.679" in rendered
    assert rendered.index("src/a.py:10") < rendered.index("def one()"), (
        "a model quoting a passage should have the reference in front of it"
    )


def test_one_oversized_chunk_cannot_crowd_out_the_others() -> None:
    hits = (
        Hit(chunk=chunk("big", "x" * (MAX_HIT_CHARS * 3)), score=0.9),
        Hit(chunk=chunk("small", "the answer"), score=0.5),
    )
    rendered = render_hits(hits, searched=("code",))
    assert "[chunk truncated]" in rendered
    assert "the answer" in rendered

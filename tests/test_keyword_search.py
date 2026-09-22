"""Keyword retrieval: Postgres full-text match on the generated tsv column, ranked by ts_rank_cd."""

import psycopg

from sovrag.auth import LEAST_PRIVILEGED
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.hybrid import search_keyword

DIM = 1024


def _meta(doc_id: str, department: str = "DND") -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title of {doc_id}",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url=f"https://example.test/{doc_id}.html",
        pages=(),
        classification="UNCLASSIFIED",
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(db: psycopg.Connection) -> None:
    """Chunk ids after truncate: a/0 -> 1, a/1 -> 2, b/0 -> 3."""
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b", department="GC_WIDE"))
    replace_chunks(
        db,
        "a",
        [
            Chunk(0, "A > x", "Shared Services Canada delivers cloud brokering for departments."),
            Chunk(
                1, "A > y", "The PSPC procurement program manages contracting for the government."
            ),
        ],
        [_unit(0), _unit(1)],
    )
    replace_chunks(
        db,
        "b",
        [Chunk(0, "B > z", "The Policy on Government Security applies to all departments.")],
        [_unit(2)],
    )


def test_exact_acronym_ranks_first(db: psycopg.Connection) -> None:
    _seed(db)
    hits = search_keyword(db, "PSPC procurement", k=5, principal=LEAST_PRIVILEGED)
    assert hits[0].chunk_id == 2
    assert all(h.score > 0 for h in hits)


def test_filler_only_query_returns_nothing(db: psycopg.Connection) -> None:
    _seed(db)
    assert search_keyword(db, "the of and", k=5, principal=LEAST_PRIVILEGED) == []


def test_hits_carry_citations(db: psycopg.Connection) -> None:
    _seed(db)
    hit = search_keyword(db, "cloud brokering", k=5, principal=LEAST_PRIVILEGED)[0]
    assert (hit.doc_id, hit.chunk_id, hit.heading_path, hit.source_url) == (
        "a",
        1,
        "A > x",
        "https://example.test/a.html",
    )


def test_k_limits_results(db: psycopg.Connection) -> None:
    _seed(db)
    assert len(search_keyword(db, "departments", k=1, principal=LEAST_PRIVILEGED)) == 1
    assert len(search_keyword(db, "departments", k=5, principal=LEAST_PRIVILEGED)) == 2

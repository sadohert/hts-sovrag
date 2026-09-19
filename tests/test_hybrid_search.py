"""Hybrid retrieval: vector and keyword rankings fused by position."""

import psycopg

from sovrag.auth import LEAST_PRIVILEGED
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.hybrid import search_hybrid, search_vector

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


def _mix(*weights: float) -> list[float]:
    """A query vector with distinct cosine distances to axes 0, 1, 2, so vector order is fixed."""
    v = [0.0] * DIM
    for axis, w in enumerate(weights):
        v[axis] = w
    return v


def _seed(db: psycopg.Connection) -> None:
    """Chunk ids after truncate: a/0 -> 1 (axis 0), a/1 -> 2 (axis 1), b/0 -> 3 (axis 2)."""
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


def test_chunk_strong_in_both_signals_ranks_first(db: psycopg.Connection) -> None:
    _seed(db)
    hits = search_hybrid(
        db, "cloud brokering", _mix(1.0, 0.5, 0.25), k=3, principal=LEAST_PRIVILEGED
    )
    assert hits[0].chunk_id == 1


def test_keyword_signal_lifts_a_weaker_vector_match(db: psycopg.Connection) -> None:
    _seed(db)
    # Vector order alone: chunk 2, then 1, then 3. Keyword matches only chunk 1.
    hits = search_hybrid(
        db, "cloud brokering", _mix(0.5, 1.0, 0.25), k=3, principal=LEAST_PRIVILEGED
    )
    assert [h.chunk_id for h in hits] == [1, 2, 3]


def test_keyword_empty_degrades_to_vector_ranking(db: psycopg.Connection) -> None:
    _seed(db)
    vec = _mix(0.25, 0.5, 1.0)
    hybrid_ids = [
        h.chunk_id for h in search_hybrid(db, "the of and", vec, k=3, principal=LEAST_PRIVILEGED)
    ]
    vector_ids = [h.chunk_id for h in search_vector(db, vec, k=3, principal=LEAST_PRIVILEGED)]
    assert hybrid_ids == vector_ids == [3, 2, 1]


def test_hybrid_score_is_the_fusion_score(db: psycopg.Connection) -> None:
    _seed(db)
    top = search_hybrid(
        db, "cloud brokering", _mix(1.0, 0.5, 0.25), k=1, principal=LEAST_PRIVILEGED
    )[0]
    assert abs(top.score - (1 / 61 + 1 / 61)) < 1e-9


def test_k_limits_results(db: psycopg.Connection) -> None:
    _seed(db)
    assert (
        len(search_hybrid(db, "departments", _mix(1.0, 0.5, 0.25), k=2, principal=LEAST_PRIVILEGED))
        == 2
    )


def test_search_dispatches_by_mode_and_embeds_only_when_needed(db: psycopg.Connection) -> None:
    from sovrag.retrieve import Mode, search

    _seed(db)
    calls: list[str] = []

    def embed_query(q: str) -> list[float]:
        calls.append(q)
        return _mix(1.0, 0.5, 0.25)

    kw = search(
        db,
        "cloud brokering",
        mode=Mode.keyword,
        k=3,
        embed_query=embed_query,
        principal=LEAST_PRIVILEGED,
    )
    assert [h.chunk_id for h in kw] == [1]
    assert calls == []

    vec = search(
        db,
        "cloud brokering",
        mode=Mode.vector,
        k=3,
        embed_query=embed_query,
        principal=LEAST_PRIVILEGED,
    )
    assert [h.chunk_id for h in vec] == [1, 2, 3]
    assert calls == ["cloud brokering"]

    hyb = search(
        db,
        "cloud brokering",
        mode=Mode.hybrid,
        k=3,
        embed_query=embed_query,
        principal=LEAST_PRIVILEGED,
    )
    assert hyb[0].chunk_id == 1
    assert calls == ["cloud brokering", "cloud brokering"]

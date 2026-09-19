"""Vector-only retrieval over the store: nearest chunks by cosine distance, with citations."""

import psycopg

from sovrag.auth import LEAST_PRIVILEGED
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document
from sovrag.retrieve.hybrid import Hit, search_vector

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
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b", department="GC_WIDE"))
    replace_chunks(
        db, "a", [Chunk(0, "A > x", "a0"), Chunk(1, "A > y", "a1")], [_unit(0), _unit(1)]
    )
    replace_chunks(db, "b", [Chunk(0, "B > z", "b0")], [_unit(2)])


def test_returns_k_nearest_chunks_ordered_by_similarity(db: psycopg.Connection) -> None:
    _seed(db)
    query = [0.0] * DIM
    query[1] = 1.0
    query[0] = 0.5
    hits = search_vector(db, query, k=2, principal=LEAST_PRIVILEGED)
    assert [h.text for h in hits] == ["a1", "a0"]
    assert hits[0].score > hits[1].score > 0


def test_hit_carries_full_citation(db: psycopg.Connection) -> None:
    _seed(db)
    (hit,) = search_vector(db, _unit(2), k=1, principal=LEAST_PRIVILEGED)
    assert isinstance(hit, Hit)
    assert hit.doc_id == "b"
    assert hit.heading_path == "B > z"
    assert hit.source_url == "https://example.test/b.html"
    assert hit.title == "Title of b"
    assert hit.department == "GC_WIDE"
    assert hit.classification == "UNCLASSIFIED"
    assert isinstance(hit.chunk_id, int)


def test_k_larger_than_corpus_returns_everything(db: psycopg.Connection) -> None:
    _seed(db)
    assert len(search_vector(db, _unit(0), k=50, principal=LEAST_PRIVILEGED)) == 3


def test_k_above_hnsw_default_ef_search_still_returns_k(db: psycopg.Connection) -> None:
    upsert_document(db, _meta("c"))
    chunks = [Chunk(i, f"C > {i}", f"c{i}") for i in range(60)]
    embeddings = [_unit(i % DIM) for i in range(60)]
    replace_chunks(db, "c", chunks, embeddings)
    hits = search_vector(db, _unit(0), k=50, principal=LEAST_PRIVILEGED)
    assert len(hits) == 50

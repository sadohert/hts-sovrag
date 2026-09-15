"""Postgres store: schema, document upsert, chunk replacement. Needs a reachable Postgres."""

import psycopg

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import init_schema, replace_chunks, upsert_document

DIM = 1024


def _meta(doc_id: str = "dnd-dp-2024-25", department: str = "DND") -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title="National Defence 2024-25 Departmental Plan",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url="https://example.test/dnd.html",
        pages=(),
        classification="UNCLASSIFIED",
    )


def _vec(seed: float) -> list[float]:
    return [float(seed)] + [0.0] * (DIM - 1)


def test_init_schema_is_idempotent(db: psycopg.Connection) -> None:
    init_schema(db)
    init_schema(db)
    tables = {
        r[0]
        for r in db.execute(
            "select table_name from information_schema.tables where table_schema='public'"
        )
    }
    assert {"documents", "chunks"} <= tables


def test_upsert_document_inserts_then_updates(db: psycopg.Connection) -> None:
    upsert_document(db, _meta())
    upsert_document(db, _meta(department="GC_WIDE"))
    rows = db.execute("select id, department, classification from documents").fetchall()
    assert rows == [("dnd-dp-2024-25", "GC_WIDE", "UNCLASSIFIED")]


def test_replace_chunks_stores_text_heading_and_embedding(db: psycopg.Connection) -> None:
    upsert_document(db, _meta())
    chunks = [Chunk(0, "A > B", "first"), Chunk(1, "A > C", "second")]
    replace_chunks(db, "dnd-dp-2024-25", chunks, [_vec(1.0), _vec(2.0)])
    rows = db.execute(
        "select ordinal, heading_path, text, (embedding::real[])[1] from chunks order by ordinal"
    ).fetchall()
    assert rows == [(0, "A > B", "first", 1.0), (1, "A > C", "second", 2.0)]


def test_replace_chunks_drops_old_chunks_for_that_document_only(db: psycopg.Connection) -> None:
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b"))
    replace_chunks(
        db, "a", [Chunk(0, "h", "a-old-1"), Chunk(1, "h", "a-old-2")], [_vec(1), _vec(1)]
    )
    replace_chunks(db, "b", [Chunk(0, "h", "b-1")], [_vec(1)])
    replace_chunks(db, "a", [Chunk(0, "h", "a-new")], [_vec(1)])
    rows = db.execute("select doc_id, text from chunks order by doc_id, ordinal").fetchall()
    assert rows == [("a", "a-new"), ("b", "b-1")]


def test_replace_chunks_requires_one_embedding_per_chunk(db: psycopg.Connection) -> None:
    import pytest

    upsert_document(db, _meta())
    with pytest.raises(ValueError):
        replace_chunks(db, "dnd-dp-2024-25", [Chunk(0, "h", "x")], [])


def test_tsvector_is_generated_from_text(db: psycopg.Connection) -> None:
    upsert_document(db, _meta())
    replace_chunks(db, "dnd-dp-2024-25", [Chunk(0, "h", "procurement of fighter jets")], [_vec(1)])
    (hit,) = db.execute(
        "select text from chunks where tsv @@ plainto_tsquery('english', 'fighter jet')"
    ).fetchone()
    assert hit == "procurement of fighter jets"

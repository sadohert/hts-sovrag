"""The fixture round-trips the index exactly and refuses a tampered file."""

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from sovrag.eval.fixture import build_fixture, load_fixture, load_question_embeddings

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document

DIM = 4


def _meta(
    doc_id: str, department: str = "DND", classification: str = "UNCLASSIFIED"
) -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title {doc_id}",
        department=department,
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url=f"https://example.test/{doc_id}",
        pages=(),
        classification=classification,
    )


def _seed(db: psycopg.Connection) -> None:
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b", department="GC_WIDE", classification="PROTECTED_B"))
    replace_chunks(
        db,
        "a",
        [Chunk(0, "A > x", "alpha text"), Chunk(1, "A > y", "beta text")],
        [[0.1, 0.2, 0.3, 0.4], [1.0, 0.0, 0.0, 0.0]],
    )
    replace_chunks(db, "b", [Chunk(0, "B > z", "gamma text")], [[0.0, 1.0, 0.0, 0.0]])


def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[float(len(t)), 0.5, 0.25, 0.125] for t in texts]


def _rows(db: psycopg.Connection) -> list[tuple[object, ...]]:
    return db.execute(
        "select doc_id, ordinal, heading_path, text, embedding::text from chunks"
        " order by doc_id, ordinal"
    ).fetchall()


@pytest.fixture
def db4(test_database_url: str) -> Iterator[psycopg.Connection]:
    """A test database whose chunks.embedding is vector(4), so fixtures stay small."""
    from pgvector.psycopg import register_vector

    with psycopg.connect(test_database_url, autocommit=True) as conn:
        conn.execute("create extension if not exists vector")
        conn.execute("drop table if exists chunks; drop table if exists documents")
        conn.execute(
            """
            create table documents (id text primary key, title text not null,
              department text not null, classification text not null, doc_type text not null,
              fiscal_year text, source_url text not null, lang text not null default 'en');
            create table chunks (id bigserial primary key,
              doc_id text not null references documents(id) on delete cascade,
              ordinal int not null, heading_path text, text text not null,
              tsv tsvector generated always as (to_tsvector('english', text)) stored,
              embedding vector(4), unique (doc_id, ordinal))
            """
        )
        register_vector(conn)
        yield conn
        conn.execute("drop table if exists chunks; drop table if exists documents")


def test_build_then_load_round_trips_exactly(db4: psycopg.Connection, tmp_path: Path) -> None:
    _seed(db4)
    before = _rows(db4)
    docs_before = db4.execute("select * from documents order by id").fetchall()

    counts = build_fixture(
        db4, tmp_path, [("q1", "alpha?"), ("q2", "gamma?")], _fake_embed, embed_model="m", dim=DIM
    )
    assert counts == {"documents": 2, "chunks": 3, "questions": 2}
    assert (tmp_path / "chunks.f32").stat().st_size == 3 * DIM * 4

    db4.execute("truncate chunks, documents restart identity cascade")
    assert load_fixture(db4, tmp_path) == {"documents": 2, "chunks": 3}
    assert _rows(db4) == before
    assert db4.execute("select * from documents order by id").fetchall() == docs_before


def test_question_embeddings_are_keyed_by_id(db4: psycopg.Connection, tmp_path: Path) -> None:
    _seed(db4)
    build_fixture(
        db4, tmp_path, [("q1", "alpha?"), ("q2", "gamma?")], _fake_embed, embed_model="m", dim=DIM
    )
    vectors = load_question_embeddings(tmp_path)
    assert vectors == {"q1": [6.0, 0.5, 0.25, 0.125], "q2": [6.0, 0.5, 0.25, 0.125]}


def test_tampered_file_is_refused(db4: psycopg.Connection, tmp_path: Path) -> None:
    _seed(db4)
    build_fixture(db4, tmp_path, [("q1", "alpha?")], _fake_embed, embed_model="m", dim=DIM)
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text(chunks.read_text().replace("alpha", "ALPHA"))
    with pytest.raises(ValueError, match=r"chunks\.jsonl"):
        load_fixture(db4, tmp_path)


def test_manifest_records_models_and_counts(db4: psycopg.Connection, tmp_path: Path) -> None:
    _seed(db4)
    build_fixture(db4, tmp_path, [("q1", "alpha?")], _fake_embed, embed_model="m", dim=DIM)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["embed_model"] == "m"
    assert manifest["dim"] == DIM
    assert set(manifest["sha256"]) == {
        "documents.jsonl",
        "chunks.jsonl",
        "chunks.f32",
        "questions.jsonl",
        "questions.f32",
    }

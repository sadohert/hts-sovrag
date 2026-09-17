"""Postgres + pgvector storage. Schema lives in schema.sql next to this module."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources

import psycopg
from pgvector.psycopg import register_vector

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta


def connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    init_schema(conn)
    return conn


def init_schema(conn: psycopg.Connection) -> None:
    sql = resources.files("sovrag.index").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.execute(sql)
    register_vector(conn)


def upsert_document(conn: psycopg.Connection, meta: DocumentMeta) -> None:
    conn.execute(
        """
        insert into documents
          (id, title, department, classification, doc_type, fiscal_year, source_url, lang)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (id) do update set
          title = excluded.title,
          department = excluded.department,
          classification = excluded.classification,
          doc_type = excluded.doc_type,
          fiscal_year = excluded.fiscal_year,
          source_url = excluded.source_url,
          lang = excluded.lang
        """,
        (
            meta.id,
            meta.title,
            meta.department,
            meta.classification,
            meta.doc_type,
            meta.fiscal_year,
            meta.source_url,
            meta.lang,
        ),
    )


def replace_chunks(
    conn: psycopg.Connection,
    doc_id: str,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
) -> None:
    if len(chunks) != len(embeddings):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")
    with conn.transaction():
        conn.execute("delete from chunks where doc_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """
                insert into chunks (doc_id, ordinal, heading_path, text, embedding)
                values (%s, %s, %s, %s, %s)
                """,
                [
                    (doc_id, c.ordinal, c.heading_path, c.text, list(e))
                    for c, e in zip(chunks, embeddings, strict=True)
                ],
            )

"""Retrieval over the pgvector store.

Three modes over the same Hit shape: vector (cosine over the HNSW index), keyword (Postgres
full-text match on the generated tsv column, ranked by ts_rank_cd) and hybrid (both, fused
by reciprocal rank fusion). The entitlement predicate comes next and is applied inside
these queries as a WHERE clause, never in Python.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from pgvector import Vector


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    doc_id: str
    ordinal: int
    heading_path: str
    text: str
    title: str
    department: str
    classification: str
    doc_type: str
    source_url: str
    score: float


_SELECT = """
select c.id, c.doc_id, c.ordinal, c.heading_path, c.text,
       d.title, d.department, d.classification, d.doc_type, d.source_url,
       {score} as score
from chunks c
join documents d on d.id = c.doc_id
"""


def search_vector(
    conn: psycopg.Connection, query_embedding: Sequence[float], *, k: int
) -> list[Hit]:
    sql = _SELECT.format(score="1 - (c.embedding <=> %(q)s)") + (
        " order by c.embedding <=> %(q)s limit %(k)s"
    )
    rows = conn.execute(sql, {"q": Vector(list(query_embedding)), "k": k}).fetchall()
    return [Hit(*row) for row in rows]


_TSQUERY = "websearch_to_tsquery('english', %(q)s)"


def search_keyword(conn: psycopg.Connection, query: str, *, k: int) -> list[Hit]:
    """Full-text match ranked by ts_rank_cd. A query of stop words only matches nothing."""
    sql = _SELECT.format(score=f"ts_rank_cd(c.tsv, {_TSQUERY})") + (
        f" where c.tsv @@ {_TSQUERY} order by score desc, c.id limit %(k)s"
    )
    rows = conn.execute(sql, {"q": query, "k": k}).fetchall()
    return [Hit(*row) for row in rows]

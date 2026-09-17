"""Retrieval over the pgvector store.

Three modes over the same Hit shape: vector (cosine over the HNSW index), keyword (Postgres
full-text match on the generated tsv column, ranked by ts_rank_cd) and hybrid (both, fused
by reciprocal rank fusion). The entitlement predicate comes next and is applied inside
these queries as a WHERE clause, never in Python.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

import psycopg
from pgvector import Vector

from sovrag.retrieve.fusion import rrf


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


class Mode(StrEnum):
    vector = "vector"
    keyword = "keyword"
    hybrid = "hybrid"


_SELECT = """
select c.id, c.doc_id, c.ordinal, c.heading_path, c.text,
       d.title, d.department, d.classification, d.doc_type, d.source_url,
       {score} as score
from chunks c
join documents d on d.id = c.doc_id
"""


# pgvector's HNSW index scan returns at most hnsw.ef_search rows (default 40); raise it
# on the connection when k exceeds the default so search_vector can return k rows.
HNSW_EF_SEARCH_DEFAULT = 40


def search_vector(
    conn: psycopg.Connection, query_embedding: Sequence[float], *, k: int
) -> list[Hit]:
    if k > HNSW_EF_SEARCH_DEFAULT:
        conn.execute(f"set hnsw.ef_search = {min(k, 1000)}")
    sql = _SELECT.format(score="1 - (c.embedding <=> %(q)s)") + (
        " order by c.embedding <=> %(q)s limit %(k)s"
    )
    rows = conn.execute(sql, {"q": Vector(list(query_embedding)), "k": k}).fetchall()
    return [Hit(*row) for row in rows]


_TSQUERY = "websearch_to_tsquery('english', %(q)s)"


def search_keyword(conn: psycopg.Connection, query: str, *, k: int) -> list[Hit]:
    """Full-text match ranked by ts_rank_cd. Every term must match: a query of stop words
    only, or any question with one term absent from a chunk, returns no rows."""
    sql = _SELECT.format(score=f"ts_rank_cd(c.tsv, {_TSQUERY})") + (
        f" where c.tsv @@ {_TSQUERY} order by score desc, c.id limit %(k)s"
    )
    rows = conn.execute(sql, {"q": query, "k": k}).fetchall()
    return [Hit(*row) for row in rows]


CANDIDATE_MIN = 20


def search_hybrid(
    conn: psycopg.Connection, query: str, query_embedding: Sequence[float], *, k: int
) -> list[Hit]:
    """Fuse vector and keyword rankings by position; the score reported is the fusion score."""
    window = max(4 * k, CANDIDATE_MIN)
    by_vector = search_vector(conn, query_embedding, k=window)
    by_keyword = search_keyword(conn, query, k=window)
    # Entitlement filtering belongs inside search_vector and search_keyword (SQL WHERE), never here.
    candidates = {h.chunk_id: h for h in [*by_vector, *by_keyword]}
    fused = rrf([[h.chunk_id for h in by_vector], [h.chunk_id for h in by_keyword]])
    return [replace(candidates[chunk_id], score=score) for chunk_id, score in fused[:k]]


def search(
    conn: psycopg.Connection,
    question: str,
    *,
    mode: Mode,
    k: int,
    embed_query: Callable[[str], Sequence[float]],
) -> list[Hit]:
    """Dispatch on mode. embed_query is called only when a vector is needed."""
    if mode is Mode.keyword:
        return search_keyword(conn, question, k=k)
    query_vec = embed_query(question)
    if mode is Mode.vector:
        return search_vector(conn, query_vec, k=k)
    return search_hybrid(conn, question, query_vec, k=k)

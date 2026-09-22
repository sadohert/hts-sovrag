"""Retrieval over the pgvector store.

Three modes over the same Hit shape: vector (cosine over the HNSW index), keyword (Postgres
full-text match on the generated tsv column, ranked by ts_rank_cd) and hybrid (both, fused
by reciprocal rank fusion). Every query carries the caller's entitlement predicate as a
WHERE clause, so a chunk the principal may not see never leaves the database (ADR 0013).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

import psycopg
from pgvector import Vector

from sovrag.auth import Principal
from sovrag.retrieve.filters import entitlement_for
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
where {entitlement}
"""


# pgvector's HNSW scan returns at most hnsw.ef_search candidates (default 40) before the
# WHERE clause is applied. search_vector raises ef_search to k and turns on iterative
# scanning for the duration of its transaction, so a filtered query still returns k rows.
HNSW_EF_SEARCH_DEFAULT = 40
HNSW_EF_SEARCH_MAX = 1000


def search_vector(
    conn: psycopg.Connection,
    query_embedding: Sequence[float],
    *,
    k: int,
    principal: Principal,
) -> list[Hit]:
    entitlement = entitlement_for(principal)
    nearest = (
        _SELECT.format(score="1 - (c.embedding <=> %(q)s)", entitlement=entitlement.sql)
        + " order by c.embedding <=> %(q)s limit %(k)s"
    )
    # relaxed_order may hand back the k rows slightly out of distance order; sort those k.
    sql = f"with nearest as materialized ({nearest}) select * from nearest order by score desc, id"
    params = {"q": Vector(list(query_embedding)), "k": k, **entitlement.params}
    ef_search = max(HNSW_EF_SEARCH_DEFAULT, min(int(k), HNSW_EF_SEARCH_MAX))
    with conn.transaction():
        conn.execute(f"set local hnsw.ef_search = {ef_search}")
        conn.execute("set local hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(sql, params).fetchall()
    return [Hit(*row) for row in rows]


_TSQUERY = "websearch_to_tsquery('english', %(q)s)"


def search_keyword(
    conn: psycopg.Connection, query: str, *, k: int, principal: Principal
) -> list[Hit]:
    """Full-text match ranked by ts_rank_cd. Every term must match: a query of stop words
    only, or any question with one term absent from a chunk, returns no rows."""
    entitlement = entitlement_for(principal)
    sql = (
        _SELECT.format(score=f"ts_rank_cd(c.tsv, {_TSQUERY})", entitlement=entitlement.sql)
        + f" and c.tsv @@ {_TSQUERY} order by score desc, c.id limit %(k)s"
    )
    params = {"q": query, "k": k, **entitlement.params}
    rows = conn.execute(sql, params).fetchall()
    return [Hit(*row) for row in rows]


CANDIDATE_MIN = 20


def search_hybrid(
    conn: psycopg.Connection,
    query: str,
    query_embedding: Sequence[float],
    *,
    k: int,
    principal: Principal,
) -> list[Hit]:
    """Fuse vector and keyword rankings by position; the score reported is the fusion score."""
    window = max(4 * k, CANDIDATE_MIN)
    by_vector = search_vector(conn, query_embedding, k=window, principal=principal)
    by_keyword = search_keyword(conn, query, k=window, principal=principal)
    # Both arms applied the entitlement predicate in SQL; fusion only orders what they returned.
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
    principal: Principal,
) -> list[Hit]:
    """Dispatch on mode. embed_query is called only when a vector is needed."""
    if mode is Mode.keyword:
        return search_keyword(conn, question, k=k, principal=principal)
    query_vec = embed_query(question)
    if mode is Mode.vector:
        return search_vector(conn, query_vec, k=k, principal=principal)
    return search_hybrid(conn, question, query_vec, k=k, principal=principal)

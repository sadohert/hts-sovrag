"""Retrieval suite: hit@5, recall@10 and MRR per mode, from precomputed question vectors.

Calls the same search functions users call, with the same HNSW settings, so the numbers
describe the real query path. The principal is the caller's choice; the CLI passes auditor,
who sees every document, so gold chunks are never filtered out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import psycopg

from sovrag.auth import Principal
from sovrag.eval.golden import RetrievalItem
from sovrag.eval.metrics import hit_at_k, mean, mrr, recall_at_k
from sovrag.eval.report import SuiteResult
from sovrag.retrieve import Mode, search

MODES = (Mode.vector, Mode.keyword, Mode.hybrid)
K = 10
_NAMES = ("hit@5", "recall@10", "mrr", "doc_hit@5")


def run_retrieval(
    conn: psycopg.Connection,
    items: Sequence[RetrievalItem],
    embeddings: Mapping[str, Sequence[float]],
    *,
    principal: Principal,
    k: int = K,
) -> SuiteResult:
    per_mode: dict[Mode, dict[str, list[float]]] = {
        mode: {name: [] for name in _NAMES} for mode in MODES
    }
    rows: list[dict[str, object]] = []
    for item in items:
        if item.id not in embeddings:
            raise KeyError(f"no question embedding for {item.id}; rebuild the fixture")
        vector = embeddings[item.id]
        gold = {(g.doc_id, g.ordinal) for g in item.gold}
        gold_docs = {g.doc_id for g in item.gold}

        def embed_query(_q: str, v: Sequence[float] = vector) -> Sequence[float]:
            return v

        for mode in MODES:
            hits = search(
                conn,
                item.question,
                mode=mode,
                k=k,
                embed_query=embed_query,
                principal=principal,
            )
            ranked = [(h.doc_id, h.ordinal) for h in hits]
            scores = {
                "hit@5": hit_at_k(ranked, gold, 5),
                "recall@10": recall_at_k(ranked, gold, 10),
                "mrr": mrr(ranked, gold),
                "doc_hit@5": hit_at_k([d for d, _ in ranked], gold_docs, 5),
            }
            for name, value in scores.items():
                per_mode[mode][name].append(value)
            rows.append(
                {"id": item.id, "mode": mode.value, "ranked": [list(r) for r in ranked], **scores}
            )
    metrics = {
        f"{mode.value}.{name}": mean(values)
        for mode, scores_by_name in per_mode.items()
        for name, values in scores_by_name.items()
    }
    return SuiteResult(metrics=metrics, rows=rows)

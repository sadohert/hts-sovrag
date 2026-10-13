"""Leak suite: a principal never retrieves a chunk from a document they may not see.

The forbidden set comes from this module's own statement of the visibility rule, not from
the SQL predicate in sovrag.retrieve.filters. An oracle that reused the predicate would
inherit its bugs; a test asserts the two agree for every named principal instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import psycopg

from sovrag.auth import GC_WIDE, Clearance, Principal
from sovrag.eval.golden import LeakItem
from sovrag.eval.report import SuiteResult
from sovrag.generate.answer import Answer
from sovrag.generate.llm import GenerationError
from sovrag.retrieve import Mode, search

MODES = (Mode.vector, Mode.keyword, Mode.hybrid)
K = 10


def visible(principal: Principal, classification: str, department: str) -> bool:
    """The visibility rule (ADR 0013), stated independently of the SQL predicate."""
    if Clearance(classification).rank > principal.clearance.rank:
        return False
    return (
        classification == Clearance.UNCLASSIFIED.value
        or department == GC_WIDE
        or department in principal.departments
    )


def forbidden_doc_ids(conn: psycopg.Connection, principal: Principal) -> frozenset[str]:
    rows = conn.execute("select id, classification, department from documents").fetchall()
    return frozenset(doc_id for doc_id, cls, dept in rows if not visible(principal, cls, dept))


def run_leak(
    conn: psycopg.Connection,
    items: Sequence[LeakItem],
    embeddings: Mapping[str, Sequence[float]],
    principals: Mapping[str, Principal],
    *,
    control: Principal,
    k: int = K,
) -> SuiteResult:
    forbidden = {name: forbidden_doc_ids(conn, p) for name, p in principals.items()}
    rows: list[dict[str, object]] = []
    leaks = 0
    checks = 0
    control_hits = 0
    for item in items:
        if item.id not in embeddings:
            raise KeyError(f"no question embedding for {item.id}; rebuild the fixture")
        vector = embeddings[item.id]

        def embed_query(_q: str, v: Sequence[float] = vector) -> Sequence[float]:
            return v

        for name, principal in principals.items():
            for mode in MODES:
                checks += 1
                hits = search(
                    conn,
                    item.question,
                    mode=mode,
                    k=k,
                    embed_query=embed_query,
                    principal=principal,
                )
                for h in hits:
                    if h.doc_id in forbidden[name]:
                        leaks += 1
                        rows.append(
                            {
                                "id": item.id,
                                "principal": name,
                                "mode": mode.value,
                                "doc_id": h.doc_id,
                                "ordinal": h.ordinal,
                            }
                        )
        gold = {(g.doc_id, g.ordinal) for g in item.gold}
        control_ranked = search(
            conn,
            item.question,
            mode=Mode.hybrid,
            k=k,
            embed_query=embed_query,
            principal=control,
        )
        hit = any((h.doc_id, h.ordinal) in gold for h in control_ranked)
        control_hits += int(hit)
        rows.append({"id": item.id, "control_hit": hit})
    metrics = {
        "leaks": float(leaks),
        "checks": float(checks),
        "positive_control": float(control_hits),
    }
    return SuiteResult(metrics=metrics, rows=rows)


def run_leak_answers(
    items: Sequence[LeakItem],
    principals: Mapping[str, Principal],
    answer_fn: Callable[[str, Principal], Answer],
) -> SuiteResult:
    """Local only: how often the generator refuses a leak question as each principal.
    Reported, not gated: visible material may legitimately overlap the question."""
    rows: list[dict[str, object]] = []
    metrics: dict[str, float] = {}
    for name, principal in principals.items():
        asked = refused = failed = 0
        for item in items:
            asked += 1
            try:
                result = answer_fn(item.question, principal)
            except GenerationError as exc:
                # One failed request (a hosted timeout, say) is a row, not the end of the run.
                failed += 1
                rows.append({"id": item.id, "principal": name, "refused": None, "error": str(exc)})
                continue
            refused += int(result.refused)
            rows.append(
                {
                    "id": item.id,
                    "principal": name,
                    "refused": result.refused,
                    "answer": result.text,
                    "citations": [c.chunk_id for c in result.citations],
                }
            )
        metrics[f"{name}.asked"] = float(asked)
        metrics[f"{name}.refused"] = float(refused)
        metrics[f"{name}.failed"] = float(failed)
    return SuiteResult(metrics=metrics, rows=rows)

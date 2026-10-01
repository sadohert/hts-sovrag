"""The retrieval runner scores every mode with precomputed question vectors and no model."""

import psycopg

from sovrag.auth import LEAST_PRIVILEGED
from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.eval.golden import GoldChunk, RetrievalItem
from sovrag.eval.retrieval import run_retrieval
from sovrag.index.store import replace_chunks, upsert_document

DIM = 1024


def _meta(doc_id: str) -> DocumentMeta:
    return DocumentMeta(
        id=doc_id,
        title=f"Title {doc_id}",
        department="GC_WIDE",
        doc_type="POLICY",
        fiscal_year=None,
        lang="en",
        source_url=f"https://example.test/{doc_id}",
        pages=(),
        classification="UNCLASSIFIED",
    )


def _unit(axis: int) -> list[float]:
    v = [0.0] * DIM
    v[axis] = 1.0
    return v


def _seed(db: psycopg.Connection) -> None:
    upsert_document(db, _meta("a"))
    upsert_document(db, _meta("b"))
    replace_chunks(
        db,
        "a",
        [Chunk(0, "A", "cloud brokering for departments"), Chunk(1, "A", "identity management")],
        [_unit(0), _unit(1)],
    )
    replace_chunks(db, "b", [Chunk(0, "B", "security screening of staff")], [_unit(2)])


def test_runner_scores_each_mode(db: psycopg.Connection) -> None:
    _seed(db)
    items = [
        RetrievalItem("q1", "cloud brokering", (GoldChunk("a", 0),), "ref", "n"),
        RetrievalItem("q2", "security screening", (GoldChunk("b", 0),), "ref", "n"),
    ]
    result = run_retrieval(db, items, {"q1": _unit(0), "q2": _unit(2)}, principal=LEAST_PRIVILEGED)
    for mode in ("vector", "keyword", "hybrid"):
        assert result.metrics[f"{mode}.hit@5"] == 1.0
        assert result.metrics[f"{mode}.recall@10"] == 1.0
        assert result.metrics[f"{mode}.mrr"] == 1.0
        assert result.metrics[f"{mode}.doc_hit@5"] == 1.0
    assert len(result.rows) == 6
    row = next(r for r in result.rows if r["id"] == "q1" and r["mode"] == "vector")
    assert row["ranked"][0] == ["a", 0]


def test_wrong_vector_scores_zero_for_vector_mode(db: psycopg.Connection) -> None:
    _seed(db)
    items = [RetrievalItem("q1", "cloud brokering", (GoldChunk("b", 0),), "ref", "n")]
    result = run_retrieval(db, items, {"q1": _unit(0)}, principal=LEAST_PRIVILEGED, k=1)
    assert result.metrics["vector.hit@5"] == 0.0
    assert result.metrics["vector.mrr"] == 0.0


def test_missing_embedding_names_the_item(db: psycopg.Connection) -> None:
    items = [RetrievalItem("q9", "x", (GoldChunk("a", 0),), "ref", "n")]
    try:
        run_retrieval(db, items, {}, principal=LEAST_PRIVILEGED)
    except KeyError as exc:
        assert "q9" in str(exc)
    else:
        raise AssertionError("expected KeyError")

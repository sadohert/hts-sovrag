"""The committed fixture matches the golden files and the manifest."""

import json
from pathlib import Path

from sovrag.eval.fixture import _read_jsonl, load_question_embeddings, read_chunks, read_documents
from sovrag.eval.golden import load_leak, load_retrieval, load_unanswerable

FIXTURE = Path("eval/fixture")
GOLDEN = Path("eval/golden")


def test_fixture_is_the_full_corpus() -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    assert manifest["documents"] == 27
    assert manifest["chunks"] == len(read_chunks(FIXTURE))
    assert manifest["dim"] == 1024
    assert manifest["embed_model"] == "hf.co/gpustack/bge-m3-gguf:Q8_0"
    assert len(read_documents(FIXTURE)) == 27


def test_every_gold_chunk_exists_in_the_fixture() -> None:
    keys = {(c["doc_id"], c["ordinal"]) for c in read_chunks(FIXTURE)}
    for item in [*load_retrieval(GOLDEN / "retrieval.jsonl"), *load_leak(GOLDEN / "leak.jsonl")]:
        for g in item.gold:
            assert (g.doc_id, g.ordinal) in keys, f"{item.id}: {g}"


def test_fixture_question_text_matches_the_golden_question_text() -> None:
    golden_by_id = {
        i.id: i.question
        for i in [
            *load_retrieval(GOLDEN / "retrieval.jsonl"),
            *load_unanswerable(GOLDEN / "unanswerable.jsonl"),
            *load_leak(GOLDEN / "leak.jsonl"),
        ]
    }
    for row in _read_jsonl(FIXTURE / "questions.jsonl"):
        assert row["question"] == golden_by_id[str(row["id"])], row["id"]


def test_every_golden_question_has_an_embedding() -> None:
    ids = {
        i.id
        for i in [
            *load_retrieval(GOLDEN / "retrieval.jsonl"),
            *load_unanswerable(GOLDEN / "unanswerable.jsonl"),
            *load_leak(GOLDEN / "leak.jsonl"),
        ]
    }
    vectors = load_question_embeddings(FIXTURE)
    assert set(vectors) == ids
    assert all(len(v) == 1024 for v in vectors.values())

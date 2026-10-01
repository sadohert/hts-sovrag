"""The committed golden files are complete, cover the corpus, and point at real documents."""

from pathlib import Path

from sovrag.corpus.manifest import load_manifest
from sovrag.eval.golden import load_leak, load_retrieval, load_unanswerable

GOLDEN = Path("eval/golden")
MANIFEST = Path("data/manifest.yaml")


def test_counts_and_unique_ids() -> None:
    docs = {d.id for d in load_manifest(MANIFEST)}
    retrieval = load_retrieval(GOLDEN / "retrieval.jsonl", known_doc_ids=docs)
    unanswerable = load_unanswerable(GOLDEN / "unanswerable.jsonl")
    leak = load_leak(GOLDEN / "leak.jsonl", known_doc_ids=docs)
    assert len(retrieval) == 30
    assert len(unanswerable) == 10
    assert len(leak) == 20
    ids = [i.id for i in [*retrieval, *unanswerable, *leak]]
    assert len(ids) == len(set(ids))


def test_retrieval_covers_every_document() -> None:
    docs = {d.id for d in load_manifest(MANIFEST)}
    covered = {g.doc_id for i in load_retrieval(GOLDEN / "retrieval.jsonl") for g in i.gold}
    assert docs <= covered


def test_leak_items_target_only_protected_documents_and_cover_them_all() -> None:
    manifest = load_manifest(MANIFEST)
    protected = {d.id for d in manifest if d.classification != "UNCLASSIFIED"}
    assert len(protected) == 13
    targeted = {g.doc_id for i in load_leak(GOLDEN / "leak.jsonl") for g in i.gold}
    assert targeted <= protected
    assert protected <= targeted


def test_every_item_has_notes_naming_what_was_read() -> None:
    for item in load_retrieval(GOLDEN / "retrieval.jsonl"):
        assert item.gold[0].doc_id in item.notes, item.id
    for leak_item in load_leak(GOLDEN / "leak.jsonl"):
        assert leak_item.gold[0].doc_id in leak_item.notes, leak_item.id

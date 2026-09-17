"""Ingest pipeline: manifest entry -> raw pages -> sections -> chunks -> embeddings -> store."""

from pathlib import Path

import psycopg

from sovrag.corpus.ingest import ingest_document
from sovrag.corpus.manifest import DocumentMeta

REAL = Path(__file__).parent / "fixtures" / "real"
DIM = 1024


class FakeEmbedder:
    dim = DIM

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(i)] + [0.0] * (DIM - 1) for i, _ in enumerate(texts)]


def _split_doc(raw_dir: Path) -> DocumentMeta:
    # a split document: landing page + one sub-page, using real fixture pages as the bytes
    (raw_dir / "dnd-dp-2024-25.html").write_bytes((REAL / "ssc-dp-2025-26.html").read_bytes())
    (raw_dir / "dnd-dp-2024-25.p01.html").write_bytes(
        (REAL / "dnd-dp-2024-25.p03.html").read_bytes()
    )
    return DocumentMeta(
        id="dnd-dp-2024-25",
        title="National Defence 2024-25 Departmental Plan",
        department="DND",
        doc_type="DP",
        fiscal_year="2024-25",
        lang="en",
        source_url="https://example.test/index.html",
        pages=("https://example.test/p1.html",),
        classification="UNCLASSIFIED",
    )


def test_ingest_stores_document_row_and_chunks_from_every_page(
    db: psycopg.Connection, tmp_path: Path
) -> None:
    meta = _split_doc(tmp_path)
    embedder = FakeEmbedder()
    n = ingest_document(db, meta, raw_dir=tmp_path, embedder=embedder)

    (doc,) = db.execute("select id, department, classification, doc_type from documents").fetchall()
    assert doc == ("dnd-dp-2024-25", "DND", "UNCLASSIFIED", "DP")
    (count,) = db.execute("select count(*) from chunks where doc_id='dnd-dp-2024-25'").fetchone()
    assert count == n > 80  # SSC page alone chunks to ~95; DND sub-page adds ~30
    heads = {r[0].split(" > ")[0] for r in db.execute("select heading_path from chunks")}
    assert heads == {"Shared Services Canada’s 2025-26 Departmental Plan", "Operations"}


def test_ingest_embeds_title_and_heading_context_but_stores_bare_text(
    db: psycopg.Connection, tmp_path: Path
) -> None:
    meta = _split_doc(tmp_path)
    embedder = FakeEmbedder()
    n = ingest_document(db, meta, raw_dir=tmp_path, embedder=embedder)
    embedded = [t for call in embedder.calls for t in call]
    stored = db.execute("select heading_path, text from chunks order by ordinal").fetchall()
    assert len(embedded) == len(stored) == n
    for text_for_embedding, (heading_path, bare_text) in zip(embedded, stored, strict=True):
        assert text_for_embedding == f"{meta.title}\n{heading_path}\n{bare_text}"
        assert not bare_text.startswith(meta.title)


def test_ingest_is_idempotent(db: psycopg.Connection, tmp_path: Path) -> None:
    meta = _split_doc(tmp_path)
    ingest_document(db, meta, raw_dir=tmp_path, embedder=FakeEmbedder())
    n = ingest_document(db, meta, raw_dir=tmp_path, embedder=FakeEmbedder())
    (count,) = db.execute("select count(*) from chunks").fetchone()
    assert count == n


def test_ingest_fails_clearly_when_a_raw_page_is_missing(
    db: psycopg.Connection, tmp_path: Path
) -> None:
    import pytest

    meta = _split_doc(tmp_path)
    (tmp_path / "dnd-dp-2024-25.p01.html").unlink()
    with pytest.raises(FileNotFoundError, match="p01"):
        ingest_document(db, meta, raw_dir=tmp_path, embedder=FakeEmbedder())


def test_embedding_text_never_exceeds_the_server_budget() -> None:
    """DMR's llama.cpp embeds at most 512 BGE-M3 tokens per input and cannot be raised.

    BGE-M3 tokenises about 11% heavier than cl100k on this corpus, so the cl100k budget for
    title + heading path + text is 400. A long heading path is trimmed from the left, keeping
    the title and the innermost headings.
    """
    from sovrag.corpus.chunk import DEFAULT_MAX_TOKENS, count_tokens
    from sovrag.corpus.ingest import EMBED_MAX_TOKENS, text_for_embedding

    meta = DocumentMeta(
        id="x",
        title="Public Services and Procurement Canada 2024 to 2025 Departmental Results Report",
        department="PSPC",
        doc_type="DRR",
        fiscal_year="2024-25",
        lang="en",
        source_url="https://example.test/x.html",
        pages=(),
        classification="PROTECTED_A",
    )
    heading = " > ".join(f"Level {i} heading with a fairly long descriptive name" for i in range(8))
    body = " ".join(f"word{i}" for i in range(2000))
    # a chunk exactly at the chunker's default ceiling
    from sovrag.corpus.chunk import chunk_sections
    from sovrag.corpus.parse import Section

    chunk = max(chunk_sections([Section(heading, body)]), key=lambda c: count_tokens(c.text))
    assert count_tokens(chunk.text) <= DEFAULT_MAX_TOKENS

    text = text_for_embedding(meta, chunk)
    assert count_tokens(text) <= EMBED_MAX_TOKENS
    assert text.startswith(meta.title)
    assert text.endswith(chunk.text)
    assert "Level 7 heading" in text  # innermost heading kept
    assert "Level 0 heading" not in text  # outermost trimmed first


def test_embedding_text_keeps_short_heading_paths_whole() -> None:
    from sovrag.corpus.chunk import Chunk
    from sovrag.corpus.ingest import text_for_embedding

    meta = DocumentMeta(
        id="x",
        title="Title",
        department="DND",
        doc_type="DP",
        fiscal_year=None,
        lang="en",
        source_url="https://example.test/x.html",
        pages=(),
        classification="UNCLASSIFIED",
    )
    assert text_for_embedding(meta, Chunk(0, "A > B", "body")) == "Title\nA > B\nbody"

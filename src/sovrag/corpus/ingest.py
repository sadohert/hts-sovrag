"""Ingest: manifest entry -> raw pages -> sections -> chunks -> embeddings -> store.

Pages of a split document are parsed in order and their sections concatenated, so chunk
ordinals run continuously through the whole document.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import psycopg

from sovrag.corpus.chunk import Chunk, chunk_sections, count_tokens
from sovrag.corpus.fetch import raw_paths
from sovrag.corpus.manifest import DocumentMeta
from sovrag.corpus.parse import Section, parse_html
from sovrag.index.store import replace_chunks, upsert_document

# cl100k budget for the embedded input. DMR's llama.cpp embeds at most 512 BGE-M3 tokens per
# input and BGE-M3 tokenises about 11% heavier than cl100k on this corpus (ADR 0005).
EMBED_MAX_TOKENS = 400


class SupportsEmbed(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def sections_for(meta: DocumentMeta, raw_dir: Path) -> list[Section]:
    sections: list[Section] = []
    for path in raw_paths({"id": meta.id, "pages": list(meta.pages)}, raw_dir):
        if not path.exists():
            raise FileNotFoundError(f"{meta.id}: raw page {path.name} not fetched")
        sections.extend(parse_html(path.read_text(encoding="utf-8")))
    return sections


def chunks_for(meta: DocumentMeta, raw_dir: Path) -> list[Chunk]:
    return chunk_sections(sections_for(meta, raw_dir))


def text_for_embedding(meta: DocumentMeta, chunk: Chunk) -> str:
    """What gets embedded: document title and heading path prepended to the chunk text.

    Split documents (DND) have sub-pages whose headings never name the department, and every
    department publishes near-identical section headings, so the bare text under-specifies
    which document and year a chunk belongs to. The stored text stays bare; only the vector
    carries the context. If the whole exceeds EMBED_MAX_TOKENS the heading path is trimmed
    from the left (outermost headings first); the title and the chunk text are never cut.
    """
    parts = chunk.heading_path.split(" > ") if chunk.heading_path else []
    while True:
        heading = " > ".join(parts)
        text = (
            f"{meta.title}\n{heading}\n{chunk.text}" if heading else f"{meta.title}\n{chunk.text}"
        )
        if count_tokens(text) <= EMBED_MAX_TOKENS or not parts:
            return text
        parts = parts[1:]


def ingest_document(
    conn: psycopg.Connection,
    meta: DocumentMeta,
    *,
    raw_dir: Path,
    embedder: SupportsEmbed,
) -> int:
    """Parse, chunk, embed and store one document. Returns the number of chunks stored."""
    chunks = chunks_for(meta, raw_dir)
    embeddings = embedder.embed([text_for_embedding(meta, c) for c in chunks])
    upsert_document(conn, meta)
    replace_chunks(conn, meta.id, chunks, embeddings)
    return len(chunks)

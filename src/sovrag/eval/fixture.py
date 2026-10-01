"""The committed index: documents, chunks and their embeddings, plus embedded golden questions.

Built from the live index after ingest, loaded into any index (CI's service Postgres) so the
retrieval and leak suites run without a model. Embeddings are raw little-endian float32,
which is exactly what pgvector stores, so the loaded index equals the built one. Every file
is sha256-checked on load; the fixture is generated, never hand-edited.
"""

from __future__ import annotations

import hashlib
import json
import sys
from array import array
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

import psycopg

from sovrag.corpus.chunk import Chunk
from sovrag.corpus.manifest import DocumentMeta
from sovrag.index.store import replace_chunks, upsert_document

FIXTURE_DIR = Path("eval/fixture")
DOC_COLUMNS = (
    "id",
    "title",
    "department",
    "classification",
    "doc_type",
    "fiscal_year",
    "source_url",
    "lang",
)
FILES = ("documents.jsonl", "chunks.jsonl", "chunks.f32", "questions.jsonl", "questions.f32")


def _write_f32(path: Path, vectors: Sequence[Sequence[float]], dim: int) -> None:
    arr = array("f")
    for v in vectors:
        if len(v) != dim:
            raise ValueError(f"embedding of length {len(v)} != dim {dim}")
        arr.extend(float(x) for x in v)
    if sys.byteorder != "little":
        arr.byteswap()
    path.write_bytes(arr.tobytes())


def _read_f32(path: Path, dim: int) -> list[list[float]]:
    arr = array("f")
    arr.frombytes(path.read_bytes())
    if sys.byteorder != "little":
        arr.byteswap()
    if len(arr) % dim:
        raise ValueError(f"{path.name}: {len(arr)} floats is not a multiple of dim {dim}")
    return [list(arr[i : i + dim]) for i in range(0, len(arr), dim)]


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _to_floats(embedding: Any) -> list[float]:
    """`register_vector` returns a pgvector `Vector` (`.to_list()`) or a plain float sequence."""
    if hasattr(embedding, "to_list"):
        embedding = embedding.to_list()
    return [float(x) for x in embedding]


def _manifest(fixture_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((fixture_dir / "manifest.json").read_text("utf-8"))
    present = set(manifest["sha256"])
    expected_files = set(FILES)
    if present != expected_files:
        missing = sorted(expected_files - present)
        extra = sorted(present - expected_files)
        detail = f"missing {missing}" if missing else f"extra {extra}"
        raise ValueError(f"manifest.json sha256 map does not match FILES: {detail}")
    for name, expected in manifest["sha256"].items():
        actual = _sha256(fixture_dir / name)
        if actual != expected:
            raise ValueError(f"{name}: sha256 {actual} != manifest {expected}; rebuild the fixture")
    return manifest


def build_fixture(
    conn: psycopg.Connection,
    out_dir: Path,
    questions: Sequence[tuple[str, str]],
    embed: Callable[[Sequence[str]], list[list[float]]],
    *,
    embed_model: str,
    dim: int,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_rows = conn.execute(
        f"select {', '.join(DOC_COLUMNS)} from documents order by id"
    ).fetchall()
    documents = [dict(zip(DOC_COLUMNS, row, strict=True)) for row in doc_rows]
    chunk_rows = conn.execute(
        "select doc_id, ordinal, heading_path, text, embedding from chunks order by doc_id, ordinal"
    ).fetchall()
    chunks = [
        {"doc_id": r[0], "ordinal": r[1], "heading_path": r[2] or "", "text": r[3]}
        for r in chunk_rows
    ]
    embeddings = [_to_floats(r[4]) for r in chunk_rows]
    question_rows = [{"id": qid, "question": text} for qid, text in questions]
    question_vectors = embed([text for _, text in questions]) if questions else []

    _write_jsonl(out_dir / "documents.jsonl", documents)
    _write_jsonl(out_dir / "chunks.jsonl", chunks)
    _write_f32(out_dir / "chunks.f32", embeddings, dim)
    _write_jsonl(out_dir / "questions.jsonl", question_rows)
    _write_f32(out_dir / "questions.f32", question_vectors, dim)
    manifest = {
        "embed_model": embed_model,
        "dim": dim,
        "documents": len(documents),
        "chunks": len(chunks),
        "questions": len(question_rows),
        "built_at": datetime.now(UTC).isoformat(),
        "sha256": {name: _sha256(out_dir / name) for name in FILES},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    return {"documents": len(documents), "chunks": len(chunks), "questions": len(question_rows)}


def read_documents(fixture_dir: Path) -> list[dict[str, str | None]]:
    _manifest(fixture_dir)
    return _read_jsonl(fixture_dir / "documents.jsonl")


def read_chunks(fixture_dir: Path) -> list[dict[str, object]]:
    _manifest(fixture_dir)
    return _read_jsonl(fixture_dir / "chunks.jsonl")


def load_fixture(conn: psycopg.Connection, fixture_dir: Path) -> dict[str, int]:
    manifest = _manifest(fixture_dir)
    documents = _read_jsonl(fixture_dir / "documents.jsonl")
    chunks = _read_jsonl(fixture_dir / "chunks.jsonl")
    if len(documents) != manifest["documents"]:
        raise ValueError(
            f"manifest documents={manifest['documents']} but documents.jsonl has {len(documents)}"
        )
    if len(chunks) != manifest["chunks"]:
        raise ValueError(f"manifest chunks={manifest['chunks']} but chunks.jsonl has {len(chunks)}")
    embeddings = _read_f32(fixture_dir / "chunks.f32", int(manifest["dim"]))
    if len(embeddings) != len(chunks):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")
    for doc in documents:
        upsert_document(conn, DocumentMeta(pages=(), **doc))
    rows = list(zip(chunks, embeddings, strict=True))
    for doc_id, group in groupby(rows, key=lambda r: str(r[0]["doc_id"])):
        pairs = list(group)
        replace_chunks(
            conn,
            doc_id,
            [Chunk(int(c["ordinal"]), str(c["heading_path"]), str(c["text"])) for c, _ in pairs],
            [e for _, e in pairs],
        )
    return {"documents": len(documents), "chunks": len(chunks)}


def load_question_embeddings(fixture_dir: Path) -> dict[str, list[float]]:
    manifest = _manifest(fixture_dir)
    questions = _read_jsonl(fixture_dir / "questions.jsonl")
    if len(questions) != manifest["questions"]:
        raise ValueError(
            f"manifest questions={manifest['questions']} but questions.jsonl has {len(questions)}"
        )
    vectors = _read_f32(fixture_dir / "questions.f32", int(manifest["dim"]))
    if len(vectors) != len(questions):
        raise ValueError(f"{len(questions)} questions but {len(vectors)} embeddings")
    return {str(q["id"]): v for q, v in zip(questions, vectors, strict=True)}

"""Golden sets: hand-written questions with the chunks that answer them.

Chunks are named by (doc_id, ordinal), never by the chunks.id serial, because ids change on
every ingest and ordinals are fixed by the deterministic chunker. Files load strictly so a
typo in a gold reference fails the suite instead of silently scoring zero.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path("eval/golden")


@dataclass(frozen=True)
class GoldChunk:
    doc_id: str
    ordinal: int


@dataclass(frozen=True)
class RetrievalItem:
    id: str
    question: str
    gold: tuple[GoldChunk, ...]
    reference_answer: str
    notes: str


@dataclass(frozen=True)
class UnanswerableItem:
    id: str
    question: str
    notes: str


@dataclass(frozen=True)
class LeakItem:
    id: str
    question: str
    gold: tuple[GoldChunk, ...]
    notes: str


def _rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    seen: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        item_id = row.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"{path}:{lineno}: missing id")
        if item_id in seen:
            raise ValueError(f"{path}:{lineno}: duplicate id {item_id}")
        seen.add(item_id)
        yield lineno, row


def _field(path: Path, lineno: int, row: dict[str, Any], name: str) -> Any:
    if name not in row:
        raise ValueError(f"{path}:{lineno}: {row['id']}: missing {name}")
    return row[name]


def _gold(
    path: Path, lineno: int, row: dict[str, Any], known_doc_ids: Collection[str] | None
) -> tuple[GoldChunk, ...]:
    raw = _field(path, lineno, row, "gold")
    if not raw:
        raise ValueError(f"{path}:{lineno}: {row['id']}: gold is empty")
    gold = tuple(GoldChunk(doc_id=str(g["doc_id"]), ordinal=int(g["ordinal"])) for g in raw)
    if known_doc_ids is not None:
        for g in gold:
            if g.doc_id not in known_doc_ids:
                raise ValueError(f"{path}:{lineno}: {row['id']}: unknown doc_id {g.doc_id}")
    return gold


def load_retrieval(
    path: Path, *, known_doc_ids: Collection[str] | None = None
) -> list[RetrievalItem]:
    return [
        RetrievalItem(
            id=row["id"],
            question=str(_field(path, n, row, "question")),
            gold=_gold(path, n, row, known_doc_ids),
            reference_answer=str(_field(path, n, row, "reference_answer")),
            notes=str(_field(path, n, row, "notes")),
        )
        for n, row in _rows(path)
    ]


def load_unanswerable(path: Path) -> list[UnanswerableItem]:
    return [
        UnanswerableItem(
            id=row["id"],
            question=str(_field(path, n, row, "question")),
            notes=str(_field(path, n, row, "notes")),
        )
        for n, row in _rows(path)
    ]


def load_leak(path: Path, *, known_doc_ids: Collection[str] | None = None) -> list[LeakItem]:
    return [
        LeakItem(
            id=row["id"],
            question=str(_field(path, n, row, "question")),
            gold=_gold(path, n, row, known_doc_ids),
            notes=str(_field(path, n, row, "notes")),
        )
        for n, row in _rows(path)
    ]

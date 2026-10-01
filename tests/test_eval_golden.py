"""Golden files load strictly: every malformed input is a ValueError that names the item."""

import json
from pathlib import Path

import pytest

from sovrag.eval.golden import GoldChunk, load_leak, load_retrieval, load_unanswerable


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8")
    return path


def _retrieval_row(item_id: str = "r01") -> dict[str, object]:
    return {
        "id": item_id,
        "question": "What is the plan?",
        "gold": [{"doc_id": "dnd-dp-2025-26", "ordinal": 3}],
        "reference_answer": "The plan is X.",
        "notes": "read dnd-dp-2025-26 ordinal 3",
    }


def test_load_retrieval_builds_items(tmp_path: Path) -> None:
    items = load_retrieval(_write(tmp_path / "r.jsonl", [_retrieval_row()]))
    assert items[0].id == "r01"
    assert items[0].gold == (GoldChunk("dnd-dp-2025-26", 3),)
    assert items[0].reference_answer == "The plan is X."


def test_missing_field_names_the_item(tmp_path: Path) -> None:
    row = _retrieval_row()
    del row["reference_answer"]
    with pytest.raises(ValueError, match=r"r01.*reference_answer"):
        load_retrieval(_write(tmp_path / "r.jsonl", [row]))


def test_duplicate_id_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate id r01"):
        load_retrieval(_write(tmp_path / "r.jsonl", [_retrieval_row(), _retrieval_row()]))


def test_empty_gold_is_an_error(tmp_path: Path) -> None:
    row = _retrieval_row()
    row["gold"] = []
    with pytest.raises(ValueError, match=r"r01.*gold"):
        load_retrieval(_write(tmp_path / "r.jsonl", [row]))


def test_unknown_gold_doc_is_an_error_when_known_ids_given(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.jsonl", [_retrieval_row()])
    with pytest.raises(ValueError, match=r"r01.*dnd-dp-2025-26"):
        load_retrieval(path, known_doc_ids={"other"})
    assert load_retrieval(path, known_doc_ids={"dnd-dp-2025-26"})


def test_load_unanswerable(tmp_path: Path) -> None:
    rows = [{"id": "u01", "question": "q", "notes": "n"}]
    assert load_unanswerable(_write(tmp_path / "u.jsonl", rows))[0].id == "u01"


def test_load_leak(tmp_path: Path) -> None:
    rows = [{"id": "l01", "question": "q", "gold": [{"doc_id": "d", "ordinal": 1}], "notes": "n"}]
    items = load_leak(_write(tmp_path / "l.jsonl", rows), known_doc_ids={"d"})
    assert items[0].gold == (GoldChunk("d", 1),)


def test_gold_entry_missing_ordinal_is_an_error(tmp_path: Path) -> None:
    row = _retrieval_row()
    row["gold"] = [{"doc_id": "d1"}]  # missing ordinal
    with pytest.raises(ValueError, match=r"r01.*ordinal"):
        load_retrieval(_write(tmp_path / "r.jsonl", [row]))


def test_gold_entry_missing_doc_id_is_an_error(tmp_path: Path) -> None:
    row = _retrieval_row()
    row["gold"] = [{"ordinal": 1}]  # missing doc_id
    with pytest.raises(ValueError, match=r"r01.*doc_id"):
        load_retrieval(_write(tmp_path / "r.jsonl", [row]))


def test_gold_entry_with_non_integer_ordinal_is_an_error(tmp_path: Path) -> None:
    row = _retrieval_row()
    row["gold"] = [{"doc_id": "d1", "ordinal": "three"}]  # non-integer ordinal
    with pytest.raises(ValueError, match=r"r01.*ordinal"):
        load_retrieval(_write(tmp_path / "r.jsonl", [row]))

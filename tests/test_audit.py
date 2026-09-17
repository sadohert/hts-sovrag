"""Audit log: one JSON line per event, appended to AUDIT_LOG_PATH."""

import json
from pathlib import Path

import pytest

from sovrag.audit import write_event


def test_appends_json_lines_with_timestamp_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "logs" / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    write_event("answer", principal="analyst_dnd", chunk_ids=[1, 2])
    write_event("search", principal="public")
    lines = [json.loads(line) for line in target.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["answer", "search"]
    assert lines[0]["principal"] == "analyst_dnd"
    assert lines[0]["chunk_ids"] == [1, 2]
    assert lines[0]["ts"].endswith("+00:00")


def test_creates_the_parent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "deep" / "er" / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    write_event("resolve")
    assert target.exists()

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


def test_concurrent_writers_never_tear_or_interleave_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight threads each append 50 records larger than the file buffer, so a write without a
    lock is split into several system calls that can interleave with another thread's."""
    import threading

    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    payload = "x" * 70_000

    def writer(n: int) -> None:
        for i in range(50):
            write_event("search", writer=n, i=i, blob=payload)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    records = [json.loads(line) for line in lines]
    assert sorted((r["writer"], r["i"]) for r in records) == sorted(
        (n, i) for n in range(8) for i in range(50)
    )
    assert all(r["blob"] == payload for r in records)

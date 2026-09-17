"""Fetcher: manifest entry -> files under data/raw, one per page, skipping files already present.

The HTTP layer is injected as a callable so tests never touch the network.
"""

from pathlib import Path

import pytest

from sovrag.corpus.fetch import (
    FetchStatusError,
    FetchTransportError,
    Getter,
    Page,
    fetch_document,
    raw_paths,
)


def _getter(routes: dict[str, str]) -> Getter:
    def get(url: str) -> Page:
        body = routes.get(url)
        if body is None:
            return Page(status=404, text="nope")
        return Page(status=200, text=body)

    return get


def test_single_page_document_is_saved_as_one_html_file(tmp_path: Path) -> None:
    doc = {"id": "ssc-dp-2025-26", "source_url": "https://example.test/ssc.html"}
    get = _getter({"https://example.test/ssc.html": "<html>ssc</html>"})
    written = fetch_document(doc, tmp_path, get=get, delay_s=0)
    assert written == [tmp_path / "ssc-dp-2025-26.html"]
    assert (tmp_path / "ssc-dp-2025-26.html").read_text() == "<html>ssc</html>"


def test_split_document_saves_landing_page_and_numbered_sub_pages(tmp_path: Path) -> None:
    doc = {
        "id": "dnd-dp-2024-25",
        "source_url": "https://example.test/index.html",
        "pages": ["https://example.test/p1.html", "https://example.test/p2.html"],
    }
    get = _getter(
        {
            "https://example.test/index.html": "index",
            "https://example.test/p1.html": "one",
            "https://example.test/p2.html": "two",
        }
    )
    written = fetch_document(doc, tmp_path, get=get, delay_s=0)
    assert written == raw_paths(doc, tmp_path)
    assert [p.name for p in written] == [
        "dnd-dp-2024-25.html",
        "dnd-dp-2024-25.p01.html",
        "dnd-dp-2024-25.p02.html",
    ]
    assert (tmp_path / "dnd-dp-2024-25.p02.html").read_text() == "two"


def test_existing_files_are_not_refetched(tmp_path: Path) -> None:
    doc = {"id": "x", "source_url": "https://example.test/x.html"}
    (tmp_path / "x.html").write_text("cached")

    def get(url: str) -> Page:
        raise AssertionError("must not be called")

    written = fetch_document(doc, tmp_path, get=get, delay_s=0)
    assert written == [tmp_path / "x.html"]
    assert (tmp_path / "x.html").read_text() == "cached"


def test_non_200_raises_and_writes_nothing(tmp_path: Path) -> None:
    doc = {"id": "x", "source_url": "https://example.test/missing.html"}
    with pytest.raises(FetchStatusError, match="404"):
        fetch_document(doc, tmp_path, get=_getter({}), delay_s=0)
    assert not (tmp_path / "x.html").exists()


def test_rejected_page_with_200_status_is_treated_as_failure(tmp_path: Path) -> None:
    """tbs-sct.canada.ca returns 'Request Rejected' with HTTP 200 to non-browser clients."""
    doc = {"id": "x", "source_url": "https://example.test/pol.aspx"}
    body = (
        "<html><head><title>Request Rejected</title></head>"
        "<body>The requested URL was rejected.</body></html>"
    )
    get = _getter({"https://example.test/pol.aspx": body})
    with pytest.raises(FetchStatusError, match="rejected"):
        fetch_document(doc, tmp_path, get=get, delay_s=0)
    assert not (tmp_path / "x.html").exists()


def test_transient_transport_error_is_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def get(url: str) -> Page:
        calls["n"] += 1
        if calls["n"] < 3:
            raise FetchTransportError("slow")
        return Page(status=200, text="eventually")

    doc = {"id": "x", "source_url": "https://example.test/x.html"}
    written = fetch_document(doc, tmp_path, get=get, delay_s=0, retry_wait_s=0)
    assert calls["n"] == 3
    assert written[0].read_text() == "eventually"


def test_gives_up_after_max_attempts(tmp_path: Path) -> None:
    calls = {"n": 0}

    def get(url: str) -> Page:
        calls["n"] += 1
        raise FetchTransportError("slow")

    doc = {"id": "x", "source_url": "https://example.test/x.html"}
    with pytest.raises(FetchTransportError):
        fetch_document(doc, tmp_path, get=get, delay_s=0, retry_wait_s=0, attempts=2)
    assert calls["n"] == 2
    assert not (tmp_path / "x.html").exists()


def test_status_errors_are_not_retried(tmp_path: Path) -> None:
    calls = {"n": 0}

    def get(url: str) -> Page:
        calls["n"] += 1
        return Page(status=500, text="boom")

    doc = {"id": "x", "source_url": "https://example.test/x.html"}
    with pytest.raises(FetchStatusError):
        fetch_document(doc, tmp_path, get=get, delay_s=0, retry_wait_s=0, attempts=3)
    assert calls["n"] == 1

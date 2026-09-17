"""Fetch manifest documents to data/raw as HTML, one file per page.

canada.ca (Akamai) and publicsafety.gc.ca throttle or drop connections from Python's own
TLS stack: plain curl and httpx hang or time out while a browser TLS fingerprint gets the
page in well under a second. Production fetching therefore goes through curl_cffi
impersonating Chrome. tbs-sct.canada.ca additionally answers "Request Rejected" with HTTP
200 to thin clients, so the body is checked as well as the status.

The HTTP layer is a plain callable (url -> Page) so it can be swapped in tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REJECTION_MARKERS = ("Request Rejected", "The requested URL was rejected")
IMPERSONATE = "chrome"


@dataclass(frozen=True)
class Page:
    status: int
    text: str


Getter = Callable[[str], Page]


class FetchTransportError(Exception):
    """Timeout, reset, TLS failure: retried."""


class FetchStatusError(Exception):
    """Non-200 status or a bot-filter rejection page: not retried."""


def browser_getter(timeout_s: float = 30.0) -> Getter:
    """A Getter backed by curl_cffi impersonating a current Chrome TLS/HTTP2 fingerprint."""
    from curl_cffi import requests as cffi_requests

    session: Any = cffi_requests.Session(impersonate=IMPERSONATE, timeout=timeout_s)

    def get(url: str) -> Page:
        try:
            response = session.get(url, allow_redirects=True)
        except cffi_requests.RequestsError as exc:
            raise FetchTransportError(f"{url}: {exc}") from exc
        return Page(status=response.status_code, text=response.text)

    return get


def page_urls(doc: Mapping[str, Any]) -> list[str]:
    return [doc["source_url"], *(doc.get("pages") or [])]


def raw_paths(doc: Mapping[str, Any], raw_dir: Path) -> list[Path]:
    """Landing page is <id>.html; sub-pages are <id>.p01.html, <id>.p02.html, ..."""
    paths = [raw_dir / f"{doc['id']}.html"]
    for i, _ in enumerate(doc.get("pages") or [], start=1):
        paths.append(raw_dir / f"{doc['id']}.p{i:02d}.html")
    return paths


def fetch_document(
    doc: Mapping[str, Any],
    raw_dir: Path,
    *,
    get: Getter,
    delay_s: float = 1.0,
    attempts: int = 3,
    retry_wait_s: float = 2.0,
) -> list[Path]:
    """Fetch every page of one manifest entry, keeping existing files. Paths are in page order.

    Transport errors are retried `attempts` times with linear backoff; status errors are not.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for url, path in zip(page_urls(doc), raw_paths(doc, raw_dir), strict=True):
        if path.exists():
            written.append(path)
            continue
        page = _get_with_retry(get, url, attempts=attempts, retry_wait_s=retry_wait_s)
        if page.status != 200:
            raise FetchStatusError(f"{url}: HTTP {page.status}")
        if any(marker in page.text[:4000] for marker in REJECTION_MARKERS):
            raise FetchStatusError(f"{url}: server rejected the request (bot filter)")
        path.write_text(page.text, encoding="utf-8")
        written.append(path)
        if delay_s:
            time.sleep(delay_s)
    return written


def _get_with_retry(get: Getter, url: str, *, attempts: int, retry_wait_s: float) -> Page:
    for attempt in range(1, attempts + 1):
        try:
            return get(url)
        except FetchTransportError:
            if attempt == attempts:
                raise
            time.sleep(retry_wait_s * attempt)
    raise AssertionError("unreachable")

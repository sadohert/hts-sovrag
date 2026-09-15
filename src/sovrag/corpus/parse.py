"""HTML -> ordered sections. Each section is the text directly under one heading path.

Works on canada.ca (WET template, content in <main>), tbs-sct.canada.ca policy pages, and
publicsafety.gc.ca. Headings h1..h6 form a stack; a heading at level n pops everything at
level >= n. Tables become one pipe-separated line per row; lists become "- item" lines.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

DROP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe")
DROP_CLASSES = ("pagedetails", "wb-share", "wb-fnote", "mfp-hide", "pagetag", "wb-inv")
HEADING_RE = re.compile(r"^h([1-6])$")
# Sections that are navigation or licence boilerplate rather than content.
SKIP_HEADINGS = frozenset({"on this page", "table of contents", "contents", "in this section"})
BLOCK_TAGS = ("p", "li", "table", "pre", "blockquote", "dt", "dd", "figcaption", "caption")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Section:
    heading_path: str
    text: str


def parse_html(html: str) -> list[Section]:
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("main") or soup.body or soup
    _strip(root)

    stack: list[tuple[int, str]] = []
    lines: list[str] = []
    sections: list[Section] = []

    def flush() -> None:
        text = "\n".join(line for line in lines if line)
        leaf = stack[-1][1].lower() if stack else ""
        if text.strip() and leaf not in SKIP_HEADINGS:
            sections.append(Section(" > ".join(h for _, h in stack), text))
        lines.clear()

    for node in _walk(root):
        m = HEADING_RE.match(node.name)
        if m:
            flush()
            level = int(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _clean(node.get_text(" "))))
        elif node.name == "table":
            lines.extend(_table_lines(node))
        elif node.name == "li":
            lines.append(f"- {_clean(node.get_text(' '))}")
        else:
            lines.append(_clean(node.get_text(" ")))
    flush()
    return sections


def _strip(root: Tag) -> None:
    for tag in root.find_all(DROP_TAGS):
        tag.decompose()
    for tag in root.find_all(class_=lambda c: bool(c) and any(x in c for x in DROP_CLASSES)):
        tag.decompose()


def _walk(root: Tag) -> Iterator[Tag]:
    """Yield headings and leaf blocks in document order, never descending into a yielded block."""
    for child in root.children:
        if isinstance(child, NavigableString) or not isinstance(child, Tag):
            continue
        if HEADING_RE.match(child.name) or child.name in BLOCK_TAGS:
            if child.name == "li" and child.find(["ul", "ol"]):
                # nested list: yield the item's own text, then recurse into the sublist
                own = _clean(
                    " ".join(
                        c if isinstance(c, str) else c.get_text(" ")
                        for c in child.children
                        if not (isinstance(c, Tag) and c.name in ("ul", "ol"))
                    )
                )
                if own:
                    yield _fake_li(own)
                yield from _walk(child)
            else:
                yield child
        else:
            yield from _walk(child)


def _fake_li(text: str) -> Tag:
    tag = BeautifulSoup("<li></li>", "lxml").li
    assert tag is not None
    tag.string = text
    return tag


def _table_lines(table: Tag) -> list[str]:
    out: list[str] = []
    caption = table.find("caption")
    if caption:
        out.append(_clean(caption.get_text(" ")))
    for row in table.find_all("tr"):
        cells = [_clean(c.get_text(" ")) for c in row.find_all(["th", "td"])]
        if any(cells):
            out.append(" | ".join(cells))
    return out


def _clean(text: str) -> str:
    return _WS.sub(" ", text).strip()

"""Heading-aware chunking.

A chunk never spans two sections (so never crosses a heading). Long sections are packed
line by line up to `max_tokens`; a single over-long line is split on words. Consecutive
chunks of the same section share roughly `overlap_tokens` of trailing text.
Token counts use tiktoken's cl100k_base as a stable, tokenizer-agnostic approximation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

from sovrag.corpus.parse import Section

_JOIN_MARGIN = 2
# Chunk text ceiling (cl100k tokens). The embedded input is title + heading path + text and must
# stay under Docker Model Runner's 512-token embedding limit; see corpus/ingest.py and ADR 0005.
DEFAULT_MAX_TOKENS = 340
DEFAULT_OVERLAP_TOKENS = 50


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading_path: str
    text: str


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def chunk_sections(
    sections: Iterable[Section],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in sections:
        for text in _split_text(section.text, max_tokens, overlap_tokens):
            chunks.append(Chunk(ordinal=len(chunks), heading_path=section.heading_path, text=text))
    return chunks


def _split_text(text: str, max_tokens: int, overlap_tokens: int) -> Iterator[str]:
    text = text.strip()
    if not text:
        return
    if count_tokens(text) <= max_tokens:
        yield text
        return

    # leave room for the carried overlap plus the newline that joins it to the next unit
    unit_limit = max_tokens - overlap_tokens - _JOIN_MARGIN
    units = [u for line in text.split("\n") for u in _split_line(line.strip(), unit_limit) if u]

    current: list[str] = []
    for unit in units:
        if current and count_tokens("\n".join([*current, unit])) > max_tokens:
            emitted = "\n".join(current)
            yield emitted
            carry = _tail(emitted, overlap_tokens)
            fits = bool(carry) and count_tokens(f"{carry}\n{unit}") <= max_tokens
            current = [carry] if fits else []
        current.append(unit)
    if current:
        yield "\n".join(current)


def _split_line(line: str, limit: int) -> list[str]:
    """Split one line into word groups of at most `limit` tokens."""
    if count_tokens(line) <= limit:
        return [line]
    out: list[str] = []
    words: list[str] = []
    for word in line.split(" "):
        if words and count_tokens(" ".join([*words, word])) > limit:
            out.append(" ".join(words))
            words = []
        words.append(word)
    if words:
        out.append(" ".join(words))
    return out


def _tail(text: str, overlap_tokens: int) -> str:
    """Trailing words of `text` totalling at most `overlap_tokens` tokens."""
    if overlap_tokens <= 0:
        return ""
    words = text.split()
    tail: list[str] = []
    for word in reversed(words):
        candidate = [word, *tail]
        if count_tokens(" ".join(candidate)) > overlap_tokens:
            break
        tail = candidate
    return " ".join(tail)

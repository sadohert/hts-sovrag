"""Heading-aware chunking: ~400 tokens, 50 overlap, never across a heading boundary."""

from sovrag.corpus.chunk import Chunk, chunk_sections, count_tokens
from sovrag.corpus.parse import Section


def _words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_short_section_becomes_exactly_one_chunk() -> None:
    chunks = chunk_sections([Section("A > B", "short text here")])
    assert chunks == [Chunk(ordinal=0, heading_path="A > B", text="short text here")]


def test_chunks_never_cross_a_heading_boundary() -> None:
    sections = [Section("A", _words(20, "a")), Section("B", _words(20, "b"))]
    chunks = chunk_sections(sections, max_tokens=400)
    assert [c.heading_path for c in chunks] == ["A", "B"]
    assert "b0" not in chunks[0].text
    assert "a0" not in chunks[1].text


def test_long_section_is_split_under_max_tokens() -> None:
    long = Section("A", _words(1200))
    chunks = chunk_sections([long], max_tokens=400, overlap_tokens=50)
    assert len(chunks) >= 3
    assert all(count_tokens(c.text) <= 400 for c in chunks)
    assert all(c.heading_path == "A" for c in chunks)


def test_consecutive_chunks_overlap_by_roughly_overlap_tokens() -> None:
    long = Section("A", _words(1200))
    chunks = chunk_sections([long], max_tokens=400, overlap_tokens=50)
    first_words = chunks[0].text.split()
    second_words = chunks[1].text.split()
    # the tail of chunk 0 reappears at the head of chunk 1
    assert second_words[0] in first_words
    shared = set(first_words) & set(second_words)
    assert 10 <= len(shared) <= 80


def test_split_prefers_line_boundaries() -> None:
    lines = [f"line {i} " + _words(30, f"l{i}") for i in range(20)]
    section = Section("A", "\n".join(lines))
    chunks = chunk_sections([section], max_tokens=200, overlap_tokens=0)
    for c in chunks:
        assert c.text.startswith("line "), c.text[:40]


def test_ordinals_are_sequential_across_sections() -> None:
    sections = [Section("A", _words(600)), Section("B", "tiny"), Section("C", _words(600))]
    chunks = chunk_sections(sections, max_tokens=400, overlap_tokens=50)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_all_source_text_is_covered() -> None:
    sections = [Section("A", _words(900, "a")), Section("B", _words(300, "b"))]
    chunks = chunk_sections(sections, max_tokens=400, overlap_tokens=50)
    seen = set(" ".join(c.text for c in chunks).split())
    assert seen == set(_words(900, "a").split()) | set(_words(300, "b").split())


def test_empty_sections_produce_no_chunks() -> None:
    assert chunk_sections([Section("A", "   \n ")]) == []

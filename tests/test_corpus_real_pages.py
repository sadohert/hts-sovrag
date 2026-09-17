"""Regression guard: parser and chunker against real page templates.

Three fetched pages are committed under tests/fixtures/real (Open Government Licence - Canada):
a canada.ca WET single-page Departmental Plan (SSC), a canada.ca DND split sub-page, and a
tbs-sct.canada.ca policy instrument. If a parser change alters how these templates are read,
the numbers here move.
"""

from pathlib import Path

import pytest

from sovrag.corpus.chunk import chunk_sections, count_tokens
from sovrag.corpus.parse import parse_html

REAL = Path(__file__).parent / "fixtures" / "real"

CASES = {
    # file: (min sections, min words, expected h1, a heading path fragment that must exist)
    "ssc-dp-2025-26.html": (
        70,
        10_000,
        "Shared Services Canada’s 2025-26 Departmental Plan",
        "Core responsibility : Common Government of Canada IT Operations",
    ),
    "dnd-dp-2024-25.p03.html": (12, 5_000, "Operations", "Operations > "),
    "directive-identity-management.html": (
        10,
        900,
        "Directive on Identity Management",
        "Directive on Identity Management > 2. Authorities",
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_real_page_parses_into_expected_structure(name: str) -> None:
    min_sections, min_words, h1, fragment = CASES[name]
    sections = parse_html((REAL / name).read_text(encoding="utf-8"))
    assert len(sections) >= min_sections
    assert sum(len(s.text.split()) for s in sections) >= min_words
    assert all(s.heading_path.startswith(h1) for s in sections)
    assert any(fragment in s.heading_path for s in sections)


@pytest.mark.parametrize("name", sorted(CASES))
def test_real_page_has_no_navigation_or_footer_text(name: str) -> None:
    sections = parse_html((REAL / name).read_text(encoding="utf-8"))
    text = "\n".join(s.text for s in sections)
    for junk in (
        "Skip to main content",
        "Date modified",
        "Terms and conditions",
        "About Canada.ca",
    ):
        assert junk not in text, junk
    assert "On this page" not in {s.heading_path.split(" > ")[-1] for s in sections}


@pytest.mark.parametrize("name", sorted(CASES))
def test_real_page_chunks_stay_under_limit_and_cover_all_sections(name: str) -> None:
    sections = parse_html((REAL / name).read_text(encoding="utf-8"))
    chunks = chunk_sections(sections, max_tokens=400, overlap_tokens=50)
    assert max(count_tokens(c.text) for c in chunks) <= 400
    assert {c.heading_path for c in chunks} == {s.heading_path for s in sections}
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))

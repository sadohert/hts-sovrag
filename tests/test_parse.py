"""HTML -> ordered sections of (heading_path, text). Structure comes from headings inside <main>."""

from pathlib import Path

from sovrag.corpus.parse import Section, parse_html

FIXTURE = Path(__file__).parent / "fixtures" / "mini_dp.html"


def _sections() -> list[Section]:
    return parse_html(FIXTURE.read_text())


def test_chrome_outside_main_is_dropped() -> None:
    text = " ".join(s.text for s in _sections())
    assert "Home" not in text
    assert "footer" not in text
    assert "var x" not in text
    assert "Date modified" not in text


def test_heading_path_nests_h1_through_h4() -> None:
    paths = [s.heading_path for s in _sections()]
    assert "Example Department 2025-26 Departmental Plan" in paths
    assert "Example Department 2025-26 Departmental Plan > From the Minister" in paths
    assert (
        "Example Department 2025-26 Departmental Plan > Core responsibilities > Operations"
        " > Planned results"
    ) in paths


def test_sibling_heading_replaces_previous_at_same_level() -> None:
    paths = [s.heading_path for s in _sections()]
    assert (
        "Example Department 2025-26 Departmental Plan > Core responsibilities > Internal services"
        in paths
    )
    assert not any("Operations > Internal services" in p for p in paths)


def test_paragraphs_under_a_heading_are_joined_with_normalised_whitespace() -> None:
    by_path = {s.heading_path: s.text for s in _sections()}
    minister = by_path["Example Department 2025-26 Departmental Plan > From the Minister"]
    assert minister == "I am pleased to present the plan.\nIt sets out our priorities for the year."


def test_list_items_become_lines() -> None:
    by_path = {s.heading_path: s.text for s in _sections()}
    ops = by_path[
        "Example Department 2025-26 Departmental Plan > Core responsibilities > Operations"
    ]
    assert "Operations delivers services to Canadians." in ops
    assert "- Priority one\n- Priority two" in ops


def test_table_rows_become_pipe_separated_lines_with_caption_and_header() -> None:
    by_path = {s.heading_path: s.text for s in _sections()}
    results = by_path[
        "Example Department 2025-26 Departmental Plan > Core responsibilities > Operations"
        " > Planned results"
    ]
    assert "Table 1: Indicators" in results
    assert "Indicator | Target | Date" in results
    assert "Uptime | 99.9% | March 2026" in results
    assert "Satisfaction | 80% | March 2026" in results


def test_headings_with_no_body_text_produce_no_section() -> None:
    # "Core responsibilities" has only sub-headings under it, no direct text.
    paths = [s.heading_path for s in _sections()]
    assert "Example Department 2025-26 Departmental Plan > Core responsibilities" not in paths


def test_sections_are_in_document_order() -> None:
    paths = [s.heading_path for s in _sections()]
    first = paths.index("Example Department 2025-26 Departmental Plan > From the Minister")
    later = paths.index(
        "Example Department 2025-26 Departmental Plan > Spending and human resources"
    )
    assert first < later


def test_falls_back_to_body_when_there_is_no_main() -> None:
    sections = parse_html("<html><body><h1>T</h1><p>hello</p></body></html>")
    assert sections == [Section(heading_path="T", text="hello")]


def test_table_of_contents_and_boilerplate_sections_are_skipped() -> None:
    html = """
    <main>
      <h1>Doc</h1>
      <h2>On this page</h2><ul><li>At a glance</li><li>From the Minister</li></ul>
      <h2>Table of contents</h2><ul><li>Section 1</li></ul>
      <h2>At a glance</h2><p>real content</p>
    </main>"""
    paths = [s.heading_path for s in parse_html(html)]
    assert paths == ["Doc > At a glance"]

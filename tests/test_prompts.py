"""Prompt construction: numbered sources, citation instruction, sentinel, firm retry."""

from sovrag.generate.prompts import REFUSAL_TEXT, SENTINEL, build_messages, format_sources
from sovrag.retrieve import Hit


def _hit(n: int) -> Hit:
    return Hit(
        chunk_id=100 + n,
        doc_id=f"doc-{n}",
        ordinal=n,
        heading_path=f"Part {n} > Section",
        text=f"Body of chunk {n}.",
        title=f"Title {n}",
        department="DND",
        classification="UNCLASSIFIED",
        doc_type="DP",
        source_url=f"https://example.test/{n}",
        score=1.0,
    )


def test_sources_are_numbered_in_order_with_title_and_heading() -> None:
    text = format_sources([_hit(1), _hit(2)])
    assert text.index("[1] Title 1 > Part 1 > Section\nBody of chunk 1.") < text.index(
        "[2] Title 2"
    )


def test_messages_carry_system_rules_and_the_question() -> None:
    messages = build_messages("What is X?", [_hit(1)])
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert "[n]" in system or "[1]" in system
    assert SENTINEL in system
    assert user.endswith("Question: What is X?")
    assert "[1] Title 1" in user


def test_firm_adds_one_sentence_and_changes_nothing_else() -> None:
    soft = build_messages("q", [_hit(1)])
    firm = build_messages("q", [_hit(1)], firm=True)
    assert firm[1] == soft[1]
    assert firm[0]["content"].startswith(soft[0]["content"])
    assert "no citations" in firm[0]["content"]


def test_refusal_text_is_a_plain_sentence() -> None:
    assert REFUSAL_TEXT.endswith(".")
    assert SENTINEL not in REFUSAL_TEXT

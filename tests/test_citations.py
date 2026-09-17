"""Citation markers: find [n], drop out-of-range ones, detect the refusal sentinel."""

from sovrag.generate.citations import is_refusal, parse
from sovrag.generate.prompts import SENTINEL


def test_finds_single_and_adjacent_markers() -> None:
    assert parse("A [1]. B [1][3].", 5) == ("A [1]. B [1][3].", (1, 3))


def test_normalises_comma_lists() -> None:
    assert parse("x [1, 3]", 5) == ("x [1][3]", (1, 3))


def test_drops_out_of_range_markers_and_tidies_punctuation() -> None:
    assert parse("A [1]. B [7].", 5) == ("A [1]. B.", (1,))
    assert parse("Only [0] here.", 5) == ("Only here.", ())


def test_deduplicates_and_sorts() -> None:
    assert parse("[3] then [1] then [3]", 5)[1] == (1, 3)


def test_no_markers_gives_empty_tuple() -> None:
    assert parse("Plain answer.", 5) == ("Plain answer.", ())


def test_refusal_detection() -> None:
    assert is_refusal(SENTINEL)
    assert is_refusal(f"  {SENTINEL}\n")
    assert is_refusal(f"I am sorry. {SENTINEL}")
    assert not is_refusal("The plan says [1].")
    assert not is_refusal("")

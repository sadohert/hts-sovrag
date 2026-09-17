"""Reciprocal rank fusion merges ranked lists by position, ignoring incomparable scores."""

import pytest

from sovrag.retrieve.fusion import DEFAULT_K, rrf


def test_chunk_in_both_lists_outranks_chunk_in_one() -> None:
    fused = rrf([[1, 2, 3], [3, 4, 5]])
    assert fused[0][0] == 3


def test_ties_break_by_chunk_id() -> None:
    fused = rrf([[7], [2]])
    assert [chunk_id for chunk_id, _ in fused] == [2, 7]


def test_empty_list_contributes_nothing() -> None:
    assert rrf([[1, 2], []]) == rrf([[1, 2]])


def test_single_list_keeps_its_order_for_any_k() -> None:
    for k in (1, DEFAULT_K, 1000):
        assert [chunk_id for chunk_id, _ in rrf([[5, 9, 1]], k=k)] == [5, 9, 1]


def test_scores_follow_the_formula() -> None:
    fused = dict(rrf([[1, 2], [2]], k=60))
    assert fused[2] == pytest.approx(1 / 62 + 1 / 61)
    assert fused[1] == pytest.approx(1 / 61)


def test_no_rankings_gives_no_results() -> None:
    assert rrf([]) == []

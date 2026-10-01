"""Retrieval metrics against hand-computed values. Keys are (doc_id, ordinal) tuples."""

from sovrag.eval.metrics import hit_at_k, mean, mrr, recall_at_k

A, B, C, D = ("a", 0), ("a", 1), ("b", 0), ("b", 1)


def test_hit_at_k_is_one_when_any_gold_is_in_top_k() -> None:
    assert hit_at_k([C, D, A], {A}, 3) == 1.0
    assert hit_at_k([C, D, A], {A}, 2) == 0.0


def test_hit_at_k_is_zero_when_nothing_matches() -> None:
    assert hit_at_k([C, D], {A, B}, 5) == 0.0


def test_recall_at_k_is_fraction_of_gold_found() -> None:
    assert recall_at_k([A, C, D], {A, B}, 3) == 0.5
    assert recall_at_k([A, B, C], {A, B}, 3) == 1.0
    assert recall_at_k([C, D, A, B], {A, B}, 2) == 0.0


def test_mrr_is_reciprocal_rank_of_first_gold() -> None:
    assert mrr([C, D, A], {A, B}) == 1 / 3
    assert mrr([B, A], {A, B}) == 1.0
    assert mrr([C], {A}) == 0.0


def test_mean_of_empty_is_zero() -> None:
    assert mean([]) == 0.0
    assert mean([1.0, 0.0]) == 0.5


def test_metrics_work_on_doc_id_strings() -> None:
    assert hit_at_k(["b", "a"], {"a"}, 2) == 1.0

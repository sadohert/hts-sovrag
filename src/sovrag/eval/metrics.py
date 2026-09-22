"""Ranking metrics. Pure functions over any hashable key; the runners use (doc_id, ordinal)."""

from __future__ import annotations

from collections.abc import Collection, Sequence


def hit_at_k[T](ranked: Sequence[T], gold: Collection[T], k: int) -> float:
    """1.0 when any gold key appears in the first k ranked keys."""
    return 1.0 if any(key in gold for key in ranked[:k]) else 0.0


def recall_at_k[T](ranked: Sequence[T], gold: Collection[T], k: int) -> float:
    """Fraction of gold keys that appear in the first k ranked keys."""
    if not gold:
        return 0.0
    found = sum(1 for key in set(ranked[:k]) if key in gold)
    return found / len(gold)


def mrr[T](ranked: Sequence[T], gold: Collection[T]) -> float:
    """Reciprocal of the 1-based rank of the first gold key; 0.0 when none is ranked."""
    for rank, key in enumerate(ranked, start=1):
        if key in gold:
            return 1.0 / rank
    return 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0

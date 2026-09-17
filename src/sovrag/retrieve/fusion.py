"""Reciprocal rank fusion.

Vector similarity and full-text rank are not on comparable scales, so hybrid retrieval
fuses by position only: each list contributes 1 / (k + rank) for every chunk it holds,
ranks starting at 1. A chunk that is high in either list rises; one that is high in both
rises furthest. k = 60 is the constant from Cormack, Clarke and Buettcher (2009); it
keeps first place from dominating.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

DEFAULT_K = 60


def rrf(rankings: Sequence[Sequence[int]], *, k: int = DEFAULT_K) -> list[tuple[int, float]]:
    """Fuse ranked lists of chunk ids. Sorted by score descending, then chunk id ascending."""
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))

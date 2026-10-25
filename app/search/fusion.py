"""Reciprocal Rank Fusion: merge ranked lists without needing comparable scores."""

from __future__ import annotations


def reciprocal_rank_fusion(ranked_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """score(id) = sum over lists of 1 / (k + rank), rank starting at 1. Returns ids sorted by score.

    Dense and BM25 scores live on different scales, so only ranks are used. An id that several lists
    agree on beats one that a single list put first. k=60 (from the original RRF paper) damps the
    advantage of the very top ranks.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

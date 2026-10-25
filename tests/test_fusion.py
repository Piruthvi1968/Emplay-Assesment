import pytest

from app.search.fusion import reciprocal_rank_fusion


def test_rrf_exact_scores() -> None:
    # dense: A, B, C   bm25: B, D, A
    fused = reciprocal_rank_fusion([["A", "B", "C"], ["B", "D", "A"]], k=60)
    scores = dict(fused)
    assert scores["B"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["A"] == pytest.approx(1 / 61 + 1 / 63)
    assert scores["D"] == pytest.approx(1 / 62)
    assert scores["C"] == pytest.approx(1 / 63)
    assert [cid for cid, _ in fused] == ["B", "A", "D", "C"]  # agreement between lists wins


def test_rrf_empty() -> None:
    assert reciprocal_rank_fusion([[], []]) == []

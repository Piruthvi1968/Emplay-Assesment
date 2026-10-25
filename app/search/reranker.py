"""Cross-encoder re-ranker (fastembed): reads question and chunk together for a precise relevance score."""

from __future__ import annotations

from fastembed.rerank.cross_encoder import TextCrossEncoder

from app.search.embedder import model_cache_dir


class Reranker:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model: TextCrossEncoder | None = None

    @property
    def model(self) -> TextCrossEncoder:
        if self._model is None:
            self._model = TextCrossEncoder(self.model_name, cache_dir=model_cache_dir())
        return self._model

    def score(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score per text (higher = more relevant; raw logits, not probabilities)."""
        if not texts:
            return []
        return [float(s) for s in self.model.rerank(query, texts)]

"""Local text embeddings with fastembed (ONNX; no PyTorch, no API key)."""

from __future__ import annotations

import os

from fastembed import TextEmbedding


def model_cache_dir() -> str | None:
    """Where fastembed stores model files; Docker sets FASTEMBED_CACHE_PATH to a baked-in folder."""
    return os.getenv("FASTEMBED_CACHE_PATH") or None


class Embedder:
    """Bi-encoder: question and chunk are embedded separately and compared by cosine similarity."""

    def __init__(self, model_name: str, batch_size: int = 32) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model: TextEmbedding | None = None

    @property
    def model(self) -> TextEmbedding:
        if self._model is None:  # lazy: loading the model costs ~1 s, only pay it when needed
            self._model = TextEmbedding(self.model_name, cache_dir=model_cache_dir())
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts, batch_size=self.batch_size)]

    def embed_query(self, text: str) -> list[float]:
        # query_embed adds the instruction prefix that bge models were trained with for queries
        return next(iter(self.model.query_embed(text))).tolist()

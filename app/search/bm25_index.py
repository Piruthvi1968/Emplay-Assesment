"""Keyword search: BM25 over chunk texts, with a tokenizer that keeps IDs and part numbers whole."""

from __future__ import annotations

import re
from collections.abc import Callable

from rank_bm25 import BM25Okapi

from app.models import Chunk

_TOKEN = re.compile(r"[a-z0-9]+(?:[-_/.#][a-z0-9]+)*")
_SEPARATORS = re.compile(r"[-_/.#]")
STOP_WORDS = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its of on or "
    "the this that to was what when where which who will with".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase tokens; 'Base 210-BLYZ' -> ['base', '210-blyz', '210', 'blyz'].

    The whole ID matches an exact query, and its parts still match a query written differently
    ('210 BLYZ'). Stop words are dropped.
    """
    out: list[str] = []
    for tok in _TOKEN.findall(text.lower()):
        if tok in STOP_WORDS:
            continue
        out.append(tok)
        if _SEPARATORS.search(tok):
            out.extend(p for p in _SEPARATORS.split(tok) if p and p not in STOP_WORDS)
    return out


class BM25Index:
    """In-memory BM25 rebuilt from the stored chunks (cheap: a few hundred chunks per bid)."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._bm25 = BM25Okapi([tokenize(c.text) for c in chunks]) if chunks else None

    def search(self, query: str, top_k: int, allow: Callable[[Chunk], bool] | None = None) -> list[tuple[str, float]]:
        """Score every chunk, keep those passing the metadata filter, return the top_k (id, score)."""
        tokens = tokenize(query)
        if self._bm25 is None or not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            (
                (c.meta.chunk_id, float(s))
                for c, s in zip(self.chunks, scores, strict=True)
                if s > 0 and (allow is None or allow(c))
            ),
            key=lambda kv: -kv[1],
        )
        return ranked[:top_k]

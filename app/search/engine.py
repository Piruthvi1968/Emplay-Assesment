"""SearchEngine: dense + BM25 retrieval, RRF fusion, exact-ID pinning, and cross-encoder re-ranking."""

from __future__ import annotations

import logging
import re
import time
from typing import Literal

from app.config import Settings, get_settings
from app.models import Chunk, QueryPlan, SearchHit, SubEvidence, SubQuery
from app.search.bm25_index import BM25Index
from app.search.embedder import Embedder
from app.search.fusion import reciprocal_rank_fusion
from app.search.reranker import Reranker
from app.search.vector_store import ChromaChunkStore, VectorStore, build_where

log = logging.getLogger(__name__)

SearchMode = Literal["vector", "hybrid", "hybrid_rerank"]

_RAW_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/.#][A-Za-z0-9]+)*")
_MAX_PINNED = 10


def id_like_tokens(text: str) -> list[str]:
    """Tokens that look like IDs/part numbers: >= 4 chars with a digit, plus a letter or '-'/'#'.

    '210-BLYZ', 'JA-207652', 'E20P4600040', 'WD22TB4' qualify; '2024', '3.1' and dates such as
    '07/09/2024' do not.
    """
    out = []
    for tok in _RAW_TOKEN.findall(text):
        if (
            len(tok) >= 4
            and any(c.isdigit() for c in tok)
            and (any(c.isalpha() for c in tok) or "-" in tok or "#" in tok)
        ):
            out.append(tok)
    return list(dict.fromkeys(out))


def _body(chunk: Chunk) -> str:
    """Chunk text without the '[Bid | doc | file | page | section]' context line."""
    text = chunk.text
    return text.split("\n", 1)[1] if text.startswith("[") and "\n" in text else text


class SearchEngine:
    def __init__(self, settings: Settings, store: VectorStore, embedder: Embedder, reranker: Reranker | None) -> None:
        self.settings = settings
        self.cfg = settings.search
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self._chunks: dict[str, Chunk] = {}
        self._bm25 = BM25Index([])
        self.refresh()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SearchEngine:
        s = settings or get_settings()
        return cls(
            s,
            VectorStore(s.paths.index_dir),
            Embedder(s.embedding.model, s.embedding.batch_size),
            Reranker(s.search.rerank_model),
        )

    def refresh(self) -> None:
        """Reload chunks from Chroma and rebuild BM25. Call after every ingest."""
        chunks = self.store.all_chunks()
        self._chunks = {c.meta.chunk_id: c for c in chunks}
        self._bm25 = BM25Index(chunks)
        log.info("search index loaded: %d chunks", len(chunks))

    def chunk_store(self) -> ChromaChunkStore:
        """The ChunkStore the ingestion pipeline writes into."""
        return ChromaChunkStore(self.store, self.embedder)

    def bids(self) -> list[str]:
        return sorted({c.meta.bid_id for c in self._chunks.values()})

    def chunks(self) -> list[Chunk]:
        return list(self._chunks.values())

    def search(
        self,
        sub_query: SubQuery | str,
        top_k: int,
        bid_ids: list[str] | None = None,
        doc_type: str | None = None,
        addendum_number: int | None = None,
        mode: SearchMode | None = None,
    ) -> list[SearchHit]:
        """Retrieve the top_k chunks for one (sub-)question, honouring the metadata filters.

        `mode` defaults to `search.default_mode` in config.yaml; the other modes exist for the eval.
        """
        start = time.perf_counter()
        mode = mode or self.cfg.default_mode
        sq = SubQuery(question=sub_query) if isinstance(sub_query, str) else sub_query
        texts = list(dict.fromkeys(t.strip() for t in [sq.question, *sq.queries, *sq.keywords] if t.strip()))
        where = build_where(bid_ids, doc_type, addendum_number)

        def allow(c: Chunk) -> bool:
            m = c.meta
            return (
                (not bid_ids or m.bid_id in bid_ids)
                and (not doc_type or m.doc_type == doc_type)
                and (addendum_number is None or m.addendum_number == addendum_number)
            )

        n = self.cfg.candidates_per_list
        ranked_lists: list[list[str]] = []
        for t in texts:
            ranked_lists.append([cid for cid, _ in self.store.query(self.embedder.embed_query(t), n, where)])
            if mode != "vector":
                ranked_lists.append([cid for cid, _ in self._bm25.search(t, n, allow)])
        fused = [(cid, s) for cid, s in reciprocal_rank_fusion(ranked_lists, self.cfg.rrf_k) if cid in self._chunks]
        rrf = dict(fused)

        if mode != "hybrid_rerank" or self.reranker is None:
            hits = [
                SearchHit(chunk=self._chunks[cid], rrf_score=s, rank=i)
                for i, (cid, s) in enumerate(fused[:top_k], start=1)
            ]
        else:
            candidates = [cid for cid, _ in fused[: self.cfg.rerank_candidates]]
            if self.cfg.enable_pinning:
                candidates += [cid for cid in self._pinned(texts, allow) if cid not in candidates]
            scores = self.reranker.score(sq.question, [self._chunks[cid].text for cid in candidates])
            by_score = dict(zip(candidates, scores, strict=True))
            order = sorted(candidates, key=lambda cid: -by_score[cid])
            if self.cfg.rerank_fusion:
                # Final order = RRF of the retrieval order and the re-rank order, so the cross-encoder
                # refines the ranking without being able to sink a passage both retrievers agreed on.
                order = [cid for cid, _ in reciprocal_rank_fusion([candidates, order], self.cfg.rrf_k)]
            hits = [
                SearchHit(chunk=self._chunks[cid], rrf_score=rrf.get(cid, 0.0), rerank_score=by_score[cid], rank=i)
                for i, cid in enumerate(order[:top_k], start=1)
            ]

        log.debug(
            "search %r mode=%s filters=%s -> %d hits in %.0f ms",
            sq.question,
            mode,
            where,
            len(hits),
            (time.perf_counter() - start) * 1000,
        )
        return hits

    def _pinned(self, texts: list[str], allow) -> list[str]:
        """Chunks containing an exact ID token from the query always reach the re-ranker.

        Dense search is weak on codes like '210-BLYZ'; BM25 usually finds them, but several query
        variants can bury them below the candidate cut-off. Pinning guarantees the re-ranker sees them.
        Tokens are matched against the chunk body only (the context line holds the bid id and file
        name, which would match every chunk). Bid ids are ignored, and a token found in more than
        _MAX_PINNED chunks is not a specific target, so it pins nothing.
        """
        bid_ids = {b.lower() for b in self.bids()}
        tokens = [
            t for t in dict.fromkeys(t.lower() for text in texts for t in id_like_tokens(text)) if t not in bid_ids
        ]  # ordered, so the cap keeps the same chunks every run
        pinned: list[str] = []
        for tok in tokens:
            matches = [c.meta.chunk_id for c in self._chunks.values() if allow(c) and tok in _body(c).lower()]
            if len(matches) <= _MAX_PINNED:
                pinned += [m for m in matches if m not in pinned]
        return pinned[:_MAX_PINNED]

    def gather_evidence(self, plan: QueryPlan) -> list[SubEvidence]:
        """Per sub-query: a main search plus an addendum-only search, so addendum changes are never crowded out."""
        subs = plan.sub_queries[: self.cfg.max_sub_queries]
        single = len(subs) == 1
        main_k = self.cfg.top_k_single if single else self.cfg.top_k_multi
        addm_k = self.cfg.addendum_k_single if single else self.cfg.addendum_k_multi
        out: list[SubEvidence] = []
        for sq in subs:
            bids = sq.bid_ids or None
            main = self.search(sq, top_k=main_k, bid_ids=bids)
            addm = self.search(sq, top_k=addm_k, bid_ids=bids, doc_type="addendum")
            hits = list({h.chunk.meta.chunk_id: h for h in [*main, *addm]}.values())
            best = max((h.rerank_score for h in hits if h.rerank_score is not None), default=None)
            no_evidence = not hits or (best is not None and best < self.cfg.not_found_threshold)
            out.append(SubEvidence(question=sq.question, hits=hits, no_evidence=no_evidence))
        return out

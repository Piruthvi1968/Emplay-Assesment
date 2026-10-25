"""Chroma persistent collection: vectors + chunk text + metadata in one local store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb

from app.models import Chunk, ChunkMeta
from app.search.embedder import Embedder

COLLECTION = "bids"


def build_where(
    bid_ids: list[str] | None = None,
    doc_type: str | None = None,
    addendum_number: int | None = None,
    exclude_doc_types: list[str] | None = None,
) -> dict[str, Any] | None:
    """Chroma `where` filter. One condition is passed bare; several are wrapped in $and."""
    conds: list[dict[str, Any]] = []
    if bid_ids:
        conds.append({"bid_id": {"$in": list(bid_ids)}} if len(bid_ids) > 1 else {"bid_id": bid_ids[0]})
    if doc_type:
        conds.append({"doc_type": doc_type})
    if exclude_doc_types:
        conds.append({"doc_type": {"$nin": list(exclude_doc_types)}})
    if addendum_number is not None:
        conds.append({"addendum_number": addendum_number})
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


class VectorStore:
    def __init__(self, path: Path, collection: str = COLLECTION) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(path))
        # embedding_function=None: we always pass our own vectors, so Chroma never embeds by itself
        self.collection = self.client.get_or_create_collection(
            collection, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        if not chunks:
            return
        self.collection.upsert(
            ids=[c.meta.chunk_id for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[c.meta.to_chroma() for c in chunks],
        )

    def delete_file(self, bid_id: str, file_name: str) -> None:
        self.collection.delete(where={"$and": [{"bid_id": bid_id}, {"file_name": file_name}]})

    def count_file(self, bid_id: str, file_name: str) -> int:
        got = self.collection.get(where={"$and": [{"bid_id": bid_id}, {"file_name": file_name}]}, include=[])
        return len(got["ids"])

    def query(
        self, embedding: list[float], n_results: int, where: dict[str, Any] | None = None
    ) -> list[tuple[str, float]]:
        """Nearest chunks as (chunk_id, cosine distance); smaller distance = more similar."""
        total = self.collection.count()
        if total == 0:
            return []
        res = self.collection.query(
            query_embeddings=[embedding], n_results=min(n_results, total), where=where, include=["distances"]
        )
        return list(zip(res["ids"][0], res["distances"][0], strict=True))

    def all_chunks(self) -> list[Chunk]:
        got = self.collection.get(include=["documents", "metadatas"])
        return [
            Chunk(meta=ChunkMeta.from_chroma(m), text=d)
            for d, m in zip(got["documents"], got["metadatas"], strict=True)
        ]

    def count(self) -> int:
        return self.collection.count()


class ChromaChunkStore:
    """The ingestion pipeline's ChunkStore backed by Chroma: embeds chunks, then upserts them."""

    def __init__(self, store: VectorStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        self.store.upsert(chunks, self.embedder.embed_documents([c.text for c in chunks]))

    def delete_file(self, bid_id: str, file_name: str) -> None:
        self.store.delete_file(bid_id, file_name)

    def count_file(self, bid_id: str, file_name: str) -> int:
        return self.store.count_file(bid_id, file_name)

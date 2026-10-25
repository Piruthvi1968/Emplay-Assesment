"""SearchEngine logic with a temp Chroma dir and fake models (no model downloads, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.models import Chunk, ChunkMeta, QueryPlan, SubQuery
from app.search.engine import SearchEngine
from app.search.vector_store import ChromaChunkStore, VectorStore
from tests.fakes import FakeEmbedder, FakeReranker


def _chunk(cid: str, bid: str, doc_type: str, text: str, addendum: int | None = None) -> Chunk:
    meta = ChunkMeta(
        chunk_id=cid, bid_id=bid, file_name=f"{cid}.pdf", doc_type=doc_type, addendum_number=addendum, page_number=1
    )
    return Chunk(meta=meta, text=text)


CHUNKS = [
    _chunk("b1_due", "Bid1", "rfp", "Solicitation due date 27-JUN-2024 2:00 PM for the proposal."),
    _chunk("b1_add2", "Bid1", "addendum", "The new due date for this RFP will be July 9, 2024.", addendum=2),
    _chunk("b1_add1", "Bid1", "addendum", "Warranty answer: one year for Chromebooks.", addendum=1),
    _chunk("b1_scope", "Bid1", "rfp", "Scope covers laptops, desktops, tablets and monitors."),
    _chunk("b2_due", "Bid2", "rfp", "Proposal due date 06/10/2024 through eMMA."),
    _chunk("b2_sku", "Bid2", "specs", "| Dell Latitude 5550 XCTO Base | 210-BLYZ |"),
] + [_chunk(f"b2_filler{i}", "Bid2", "affidavit", f"Affidavit clause {i} about workplace rules.") for i in range(30)]


@pytest.fixture
def engine(tmp_path: Path, settings: Settings) -> SearchEngine:
    settings.search.candidates_per_list = 3  # small lists so pinning has something to rescue
    settings.search.rerank_candidates = 3
    store = VectorStore(tmp_path / "chroma")
    ChromaChunkStore(store, FakeEmbedder()).upsert_chunks(CHUNKS)
    return SearchEngine(settings, store, FakeEmbedder(), FakeReranker())


def test_bid_and_doc_type_filters(engine: SearchEngine) -> None:
    hits = engine.search("due date", top_k=5, bid_ids=["Bid2"])
    assert hits and all(h.chunk.meta.bid_id == "Bid2" for h in hits)
    hits = engine.search("due date", top_k=5, bid_ids=["Bid1"], doc_type="addendum")
    assert {h.chunk.meta.chunk_id for h in hits} <= {"b1_add2", "b1_add1"}
    assert engine.search("due date", top_k=5, bid_ids=["Bid2"], doc_type="addendum") == []
    base_only = engine.search("new due date addendum", top_k=10, bid_ids=["Bid1"], exclude_doc_types=["addendum"])
    assert base_only and all(h.chunk.meta.doc_type != "addendum" for h in base_only)


def test_document_chunks_in_reading_order(engine: SearchEngine) -> None:
    docs = engine.document_chunks("Bid1", "addendum")
    assert sorted(docs) == ["b1_add1.pdf", "b1_add2.pdf"]
    assert engine.get_chunk("b2_sku").meta.doc_type == "specs" and engine.get_chunk("nope") is None


def test_modes_and_ranks(engine: SearchEngine) -> None:
    for mode in ("vector", "hybrid", "hybrid_rerank"):
        hits = engine.search("new due date", top_k=3, bid_ids=["Bid1"], mode=mode)
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
        assert (hits[0].rerank_score is not None) == (mode == "hybrid_rerank")


def test_pinning_rescues_exact_id(engine: SearchEngine) -> None:
    # Several rewritten queries about 'affidavit clauses' push the filler chunks to the top of the
    # fused list, so the SKU chunk misses the 3 re-rank slots. Pinning must still bring it in.
    sq = SubQuery(
        question="210-BLYZ",
        queries=[
            "affidavit clause workplace",
            "workplace rules affidavit",
            "affidavit clause rules",
            "clause workplace rules",
        ],
    )
    hits = engine.search(sq, top_k=3, bid_ids=["Bid2"])
    assert hits[0].chunk.meta.chunk_id == "b2_sku"
    engine.cfg.enable_pinning = False
    hits = engine.search(sq, top_k=3, bid_ids=["Bid2"])
    assert "b2_sku" not in {h.chunk.meta.chunk_id for h in hits}  # lost without pinning


def test_pinning_ignores_context_line_bid_ids_and_common_tokens(engine: SearchEngine) -> None:
    allow = lambda c: True  # noqa: E731
    # 'Bid2' is a bid id, not a part number
    assert engine._pinned(["Bid2 details"], allow) == []
    # an ID that only appears in the context line (file name) pins nothing
    engine._chunks["b2_sku"].text = "[Bid2 | Specs | QX-9000 quote | p.1]\n| Dell Latitude 5550 | 210-BLYZ |"
    assert engine._pinned(["QX-9000"], allow) == []
    assert engine._pinned(["210-BLYZ"], allow) == ["b2_sku"]
    # a token present in more chunks than the cap is not a specific target
    for i in range(12):
        engine._chunks[f"b2_filler{i}"].text = f"Clause {i} references form AB-12."
    assert engine._pinned(["AB-12"], allow) == []


def test_gather_evidence_includes_addendum(engine: SearchEngine) -> None:
    plan = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="due date", bid_ids=["Bid1"])])
    ev = engine.gather_evidence(plan)
    assert len(ev) == 1
    ids = [h.chunk.meta.chunk_id for h in ev[0].hits]
    assert "b1_add2" in ids and len(ids) == len(set(ids))  # addendum present, no duplicates
    # a bid with no addenda just gets the main hits
    plan = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="due date", bid_ids=["Bid2"])])
    assert all(h.chunk.meta.doc_type != "addendum" for h in engine.gather_evidence(plan)[0].hits)


def test_not_found_flag(engine: SearchEngine) -> None:
    engine.cfg.not_found_threshold = 0.0
    plan = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="zzz qqq", bid_ids=["Bid1"])])
    assert engine.gather_evidence(plan)[0].no_evidence


def test_refresh_and_delete(engine: SearchEngine) -> None:
    assert engine.bids() == ["Bid1", "Bid2"]
    engine.store.delete_file("Bid2", "b2_sku.pdf")
    engine.refresh()
    assert "b2_sku" not in {c.meta.chunk_id for c in engine.chunks()}


def test_non_positive_top_k_returns_nothing(engine: SearchEngine) -> None:
    assert engine.search("due date", top_k=0) == [] and engine.search("due date", top_k=-1) == []

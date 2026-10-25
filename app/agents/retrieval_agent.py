"""Retrieval / Search agent: builds the search queries and calls the search engine (the agents' tool)."""

from __future__ import annotations

import time

from app.agents.base import AgentContext, agent_node
from app.agents.state import BidState
from app.config import FieldSpec
from app.models import QueryPlan, SearchHit, SubQuery


def diversify(hits: list[SearchHit], top_k: int, max_per_file: int) -> list[SearchHit]:
    """Keep rank order but at most `max_per_file` chunks per document, so one long document can't take every slot.

    Unused slots are filled with the remaining hits in rank order.
    """
    picked: list[SearchHit] = []
    per_file: dict[str, int] = {}
    for hit in hits:
        name = hit.chunk.meta.file_name
        if per_file.get(name, 0) < max_per_file:
            picked.append(hit)
            per_file[name] = per_file.get(name, 0) + 1
    picked += [h for h in hits if h not in picked]
    return picked[:top_k]


class RetrievalAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def field_query(self, spec: FieldSpec, wide: bool) -> SubQuery:
        """The field's question plus its hints as rephrasings; a retry adds the synonym expansions."""
        queries = list(spec.hints)
        if wide:
            text = f"{spec.question} {' '.join(spec.hints)}".lower()
            queries += [
                " ".join(phrases)
                for key, phrases in self.ctx.settings.synonyms.items()
                if key in text or any(p.lower() in text for p in phrases)
            ]
        return SubQuery(question=spec.question, queries=queries)

    def search(self, sq: SubQuery, top_k: int, **filters: object) -> list[SearchHit]:
        start = time.perf_counter()
        hits = self.ctx.engine.search(sq, top_k=top_k, **filters)
        self.ctx.trace.log(
            "retrieval",
            "tool_call",
            tool="search",
            question=sq.question,
            n_queries=1 + len(sq.queries),
            filters={k: v for k, v in filters.items() if v},
            top_k=top_k,
            n_hits=len(hits),
            ms=round((time.perf_counter() - start) * 1000),
            hits=[f"{h.chunk.meta.file_name} p.{h.chunk.meta.page_number}" for h in hits],
        )
        return hits

    def openings(self, bid_id: str) -> list[SearchHit]:
        """The first chunks of the main solicitation and the bid portal page: cover pages carry the key facts
        (title, identifiers, dates, buyer, issuer), whatever a search ranks highest."""
        n = self.ctx.settings.agents.opening_chunks_per_document
        return [
            SearchHit(chunk=c)
            for doc_type in ("rfp", "bid_page")
            for chunks in self.ctx.engine.document_chunks(bid_id, doc_type).values()
            for c in chunks[:n]
        ]

    @agent_node("retrieval")
    def for_fields(self, state: BidState) -> dict:
        """Evidence per field from the base documents. Addenda are read whole by the reconciliation agent."""
        cfg = self.ctx.settings.agents
        attempts = dict(state["attempts"])
        evidence: dict[str, list[SearchHit]] = {}
        for name in state["fields_todo"]:
            attempts[name] = attempts.get(name, 0) + 1
            wide = attempts[name] > 1
            sq = self.field_query(self.ctx.specs[name], wide)
            top_k = cfg.extraction_top_k_retry if wide else cfg.extraction_top_k
            hits = self.search(sq, 2 * top_k, bid_ids=[state["bid_id"]], exclude_doc_types=["addendum"])
            evidence[name] = diversify(hits, top_k, cfg.extraction_max_per_file)
        return {
            "evidence": evidence,
            "openings": state.get("openings") or self.openings(state["bid_id"]),
            "attempts": attempts,
            "extraction_issues": {name: [] for name in state["fields_todo"]},  # clear last round's issues
        }

    @agent_node("qa_retrieval")
    def for_question(self, state: BidState) -> dict:
        """Main + addendum evidence per sub-query; on a follow-up round, extra searches the answer agent asked for."""
        plan = state["plan"]
        if state.get("follow_ups"):
            bids = sorted({b for sq in plan.sub_queries for b in sq.bid_ids})
            plan = QueryPlan(
                intent=plan.intent, sub_queries=[SubQuery(question=q, bid_ids=bids) for q in state["follow_ups"]]
            )
        start = time.perf_counter()
        evidence = self.ctx.engine.gather_evidence(plan)
        self.ctx.trace.log(
            "qa_retrieval",
            "tool_call",
            tool="gather_evidence",
            sub_queries=[sq.question for sq in plan.sub_queries],
            n_hits=[len(e.hits) for e in evidence],
            ms=round((time.perf_counter() - start) * 1000),
        )
        if state.get("follow_ups"):
            return {"qa_evidence": state["qa_evidence"] + evidence, "qa_round": state["qa_round"] + 1, "follow_ups": []}
        return {"qa_evidence": evidence}

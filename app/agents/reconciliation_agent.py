"""Addendum reconciliation agent: reads each addendum in order and applies the changes it makes to the fields."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from app.agents.base import AgentContext, agent_node
from app.agents.evidence import number_evidence
from app.agents.extraction_agent import normalize_value
from app.agents.prompts import RECONCILE_PROMPT, RECONCILE_SYSTEM
from app.agents.state import BidState
from app.ingestion.chunker import doc_label
from app.llm_client import LLMError
from app.models import AddendumChange, Chunk, FieldResult, FieldValue, SearchHit, Source, SubEvidence


class ProposedChange(BaseModel):
    field: str
    change_type: Literal["override", "clarification"]
    new_value: str | list[str] | None
    evidence: int
    quote: str
    reason: str


class AddendumReview(BaseModel):
    changes: list[ProposedChange]


def _show(value: FieldValue) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def _sentences(*parts: str) -> str:
    """Join note fragments, ending each with a full stop."""
    cleaned = [p.strip() for p in parts if p and p.strip()]
    return " ".join(p if p[-1] in ".!?" else f"{p}." for p in cleaned)


class ReconciliationAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def addenda(self, bid_id: str) -> list[tuple[str, list[Chunk]]]:
        """(label, chunks) per addendum, ordered by addendum number, then document date, then file name."""
        docs = self.ctx.engine.document_chunks(bid_id, "addendum")

        def order(item: tuple[str, list[Chunk]]) -> tuple:
            meta = item[1][0].meta
            return (meta.addendum_number if meta.addendum_number is not None else 10**6, meta.doc_date or "", item[0])

        return [
            (doc_label("addendum", chunks[0].meta.addendum_number), chunks)
            for _, chunks in sorted(docs.items(), key=order)
        ]

    @agent_node("reconciliation")
    def __call__(self, state: BidState) -> dict:
        todo = state["fields_todo"]
        kept = [c for c in state.get("addendum_changes", []) if c.field not in todo]  # frozen fields keep theirs
        addenda = self.addenda(state["bid_id"])
        if not addenda:
            self.ctx.trace.log("reconciliation", "decision", note="no addenda in this bid")
            return {"addendum_changes": kept}

        drafts = {f: state["drafts"].get(f, FieldResult()).model_copy(deep=True) for f in todo}
        changes: list[AddendumChange] = []
        batch_size = self.ctx.settings.agents.reconcile_batch_chunks
        for label, chunks in addenda:
            for i in range(0, len(chunks), batch_size):
                changes += self.review(state["bid_id"], label, chunks[i : i + batch_size], drafts)
        return {"drafts": drafts, "addendum_changes": kept + changes}

    def review(self, bid_id: str, label: str, chunks: list[Chunk], drafts: dict[str, FieldResult]) -> list:
        """One LLM call: which of the fields does this (part of the) addendum change? Applies them to drafts."""
        specs = self.ctx.specs
        text, by_number = number_evidence(
            [SubEvidence(question=label, hits=[SearchHit(chunk=c) for c in chunks])], label="Document"
        )
        fields = "\n".join(
            f"- {name} ({specs[name].type}{'; format ' + specs[name].format if specs[name].format else ''}): "
            f"{_show(draft.value)}\n  question: {specs[name].question}"
            for name, draft in drafts.items()
        )
        prompt = RECONCILE_PROMPT.format(bid_id=bid_id, addendum=label, fields=fields, evidence=text)
        try:
            result = self.ctx.llm.structured(
                prompt, AddendumReview, system=RECONCILE_SYSTEM, role="strong", agent="reconciliation"
            )
        except LLMError as exc:
            self.ctx.trace.log("reconciliation", "decision", addendum=label, status="error", error=str(exc))
            return []

        applied: list[AddendumChange] = []
        for ch in result.changes:
            hit = by_number.get(ch.evidence)
            if ch.field not in drafts or hit is None:
                self.ctx.trace.log(
                    "reconciliation",
                    "decision",
                    addendum=label,
                    skipped=ch.field,
                    reason="unknown field or evidence number",
                )
                continue
            m = hit.chunk.meta
            source = Source(
                file=m.file_name, page=m.page_number, section=m.section, chunk_id=m.chunk_id, quote=ch.quote.strip()
            )
            applied.append(self.apply(drafts[ch.field], ch, source, label, specs[ch.field].type))
        self.ctx.trace.log(
            "reconciliation",
            "decision",
            addendum=label,
            chunks=len(chunks),
            changes=[f"{c.field}: {c.change_type}" for c in applied],
        )
        return applied

    @staticmethod
    def apply(draft: FieldResult, ch: ProposedChange, source: Source, label: str, field_type: str) -> AddendumChange:
        """Override: the addendum value replaces the old one and becomes the only source.
        Clarification: the value may be refined; the addendum is added as an extra source."""
        old = draft.value
        new = normalize_value(ch.new_value, field_type)
        if old is None and new is not None:  # the addendum supplies what the base documents lacked
            draft.notes = draft.notes.replace("Not found in documents", "").strip(" .")
            draft.confidence = max(draft.confidence, 0.85)
        if ch.change_type == "override" and new is not None:
            draft.value, draft.sources = new, [source]
            draft.confidence = max(draft.confidence, 0.9)  # an explicit replacement in a later official document
            earlier = f"Notes on the original value: {draft.notes}" if draft.notes else ""
            draft.notes = _sentences(f"Changed by {label} (original: {_show(old)})", ch.reason, earlier)
        else:
            if new is not None:
                draft.value = new
            if draft.value is not None:  # a null value carries no sources; the clarification lives in the notes
                draft.sources = [*draft.sources, source]
            draft.notes = _sentences(draft.notes, f"{label}: {ch.reason}")
        return AddendumChange(
            field=ch.field,
            change_type=ch.change_type,
            old_value=old,
            new_value=draft.value,
            addendum=label,
            source=source,
        )

"""Q&A / Report agent: cited answers to questions, and the final JSON record + Bid Summary for extraction."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import UTC, datetime

from pydantic import BaseModel

from app.agents.base import AgentContext, agent_node
from app.agents.evidence import NOT_FOUND, CitedQuote, number_evidence, quote_supported, to_source
from app.agents.prompts import ANSWER_PROMPT, ANSWER_SYSTEM, SUMMARY_PROMPT, SUMMARY_SYSTEM
from app.agents.state import BidState
from app.config import Settings
from app.llm_client import LLM, LLMError
from app.models import (
    AnswerPart,
    BidRecord,
    FieldResult,
    FieldValidation,
    QAResponse,
    QueryPlan,
    SearchHit,
    Source,
    SubEvidence,
)
from app.search.engine import SearchEngine, chunk_body

_MARKER = re.compile(r"\[(\d+)\]")


class DraftPart(BaseModel):
    sub_question: str
    answer: str | None
    citations: list[CitedQuote]


class DraftAnswer(BaseModel):
    """What the LLM returns; numbers refer to the evidence blocks in the prompt."""

    answer: str
    parts: list[DraftPart]
    follow_up_queries: list[str] = []


class SummaryDraft(BaseModel):
    summary: str
    citations: list[int]


def answer_question(
    llm: LLM, question: str, plan: QueryPlan, evidence: list[SubEvidence], run_id: str
) -> tuple[QAResponse, list[str]]:
    """Answer from evidence only; citation numbers are mapped back to files/pages by code.

    Also returns the follow-up searches the model asked for (empty when the evidence was enough).
    """
    if plan.intent in ("chitchat", "out_of_scope"):
        return QAResponse(question=question, intent=plan.intent, answer=plan.reply or "", run_id=run_id), []
    if not any(sub.hits for sub in evidence):
        parts = [AnswerPart(sub_question=s.question) for s in evidence]
        return QAResponse(question=question, intent=plan.intent, answer=NOT_FOUND, run_id=run_id, parts=parts), []

    text, by_number = number_evidence(evidence)
    prompt = ANSWER_PROMPT.format(question=question, evidence=text)
    try:
        draft = llm.structured(prompt, DraftAnswer, system=ANSWER_SYSTEM, role="strong", agent="qa_answer")
    except LLMError as exc:
        answer = f"The answer could not be generated ({exc})."
        return QAResponse(question=question, intent=plan.intent, run_id=run_id, answer=answer), []
    return _resolve(draft, question, plan.intent, by_number, run_id), draft.follow_up_queries[:2]


def _resolve(
    draft: DraftAnswer, question: str, intent: str, by_number: dict[int, SearchHit], run_id: str
) -> QAResponse:
    """Keep only evidence numbers that exist, renumber them 1..k in order of use, attach verified quotes."""
    quotes: dict[int, str] = {}
    for part in draft.parts:
        for c in part.citations:
            quotes.setdefault(c.evidence, c.quote)
    used = [n for n in dict.fromkeys([*map(int, _MARKER.findall(draft.answer)), *quotes]) if n in by_number]
    renumber = {old: new for new, old in enumerate(used, start=1)}
    sources: dict[int, Source] = {n: to_source(by_number[n].chunk, quotes.get(n, "")) for n in used}

    def remap(match: re.Match[str]) -> str:
        n = int(match.group(1))
        return f"[{renumber[n]}]" if n in renumber else ""

    parts = [
        AnswerPart(
            sub_question=p.sub_question,
            answer=p.answer,
            citations=[sources[c.evidence] for c in p.citations if c.evidence in sources],
        )
        for p in draft.parts
    ]
    return QAResponse(
        question=question,
        intent=intent,
        answer=re.sub(r"\s+([.,;:])", r"\1", _MARKER.sub(remap, draft.answer)).strip(),
        parts=parts,
        citations=[sources[n] for n in used],
        run_id=run_id,
    )


def with_verified_quotes(result: FieldResult, engine: SearchEngine) -> FieldResult:
    """Final guard for the JSON file: a quote that is not in its chunk is replaced by the chunk's own text."""
    sources = []
    for src in result.sources:
        chunk = engine.get_chunk(src.chunk_id)
        if chunk is not None and not quote_supported(src.quote, chunk_body(chunk)):
            src = src.model_copy(update={"quote": chunk_body(chunk)[:300].strip()})
        sources.append(src)
    return result.model_copy(update={"sources": sources})


def fallback_record(bid_id: str, settings: Settings, error: str, run_id: str) -> BidRecord:
    """The JSON written when the run itself breaks: every field present, failed, with the reason."""
    fields = {f.name: FieldResult(notes=f"Run failed: {error}") for f in settings.fields}
    checks = {f.name: FieldValidation(status="failed", failures=[error]) for f in settings.fields}
    return BidRecord(
        bid_id=bid_id,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        fields=fields,
        validation={"passed": 0, "failed": len(fields), "not_found": 0},
        field_validation=checks,
        run_info={"run_id": run_id, "errors": [error]},
    )


def save_record(record: BidRecord, settings: Settings) -> None:
    path = settings.paths.outputs_dir / f"{record.bid_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")


class ReportAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    @agent_node("qa_answer")
    def answer(self, state: BidState) -> dict:
        response, follow_ups = answer_question(
            self.ctx.llm, state["question"], state["plan"], state["qa_evidence"], state["run_id"]
        )
        if follow_ups and state.get("qa_round", 0) < self.ctx.settings.agents.qa_follow_up_rounds:
            self.ctx.trace.log("qa_answer", "decision", follow_ups=follow_ups)
            return {"follow_ups": follow_ups}
        return {"final_output": response, "follow_ups": []}

    def summary(
        self, bid_id: str, fields: dict[str, FieldResult], checks: dict[str, FieldValidation]
    ) -> tuple[FieldResult, FieldValidation]:
        """Bid Summary written from the validated fields only (status passed), citing them as numbered facts."""
        facts: list[tuple[str, FieldResult]] = [
            (n, r) for n, r in fields.items() if r.value is not None and r.sources and checks[n].status == "passed"
        ]
        lines = [f"[{i}] {name}: {json.dumps(r.value, ensure_ascii=False)}" for i, (name, r) in enumerate(facts, 1)]
        missing = [n for n, r in fields.items() if r.value is None]
        if missing:
            lines.append(f"Not found in the documents: {', '.join(missing)}")
        try:
            draft = self.ctx.llm.structured(
                SUMMARY_PROMPT.format(bid_id=bid_id, facts="\n".join(lines)),
                SummaryDraft,
                system=SUMMARY_SYSTEM,
                role="strong",
                agent="report_summary",
            )
        except LLMError as exc:
            reason = f"summary could not be generated: {exc}"
            return FieldResult(notes=reason), FieldValidation(status="failed", failures=[reason])
        numbers = [*draft.citations, *map(int, _MARKER.findall(draft.summary))]  # the list and the [n] markers
        cited = [facts[n - 1] for n in dict.fromkeys(numbers) if 1 <= n <= len(facts)]
        sources = list({r.sources[0].chunk_id: r.sources[0] for _, r in cited}.values())
        text = _MARKER.sub("", draft.summary)
        text = re.sub(r"\s+([.,;:])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
        if not text or not sources:
            check = FieldValidation(status="failed", confidence=0.3, failures=["summary cites none of the facts"])
            return FieldResult(value=text or None, sources=sources, confidence=0.3), check
        notes = f"Generated from the validated fields: {', '.join(n for n, _ in cited)}."
        return (
            FieldResult(value=text, sources=sources, confidence=0.9, notes=notes),
            FieldValidation(status="passed", confidence=0.9),
        )

    def _fallback(self, state: dict, exc: Exception) -> dict:
        record = fallback_record(state["bid_id"], self.ctx.settings, f"report failed: {exc}", state["run_id"])
        save_record(record, self.ctx.settings)
        return {"final_output": record}

    @agent_node("report", on_error=_fallback)
    def report(self, state: BidState) -> dict:
        settings, engine = self.ctx.settings, self.ctx.engine
        drafts, checks = state.get("drafts", {}), dict(state.get("validation", {}))
        fields: dict[str, FieldResult] = {}
        for spec in settings.fields:
            if spec.type == "summary":
                continue
            fields[spec.name] = with_verified_quotes(drafts.get(spec.name, FieldResult(notes="not processed")), engine)
            if spec.name not in checks:
                checks[spec.name] = FieldValidation(status="failed", failures=["not processed"])
        for spec in settings.fields:  # generated last, from the final field values
            if spec.type == "summary":
                fields[spec.name], checks[spec.name] = self.summary(state["bid_id"], fields, checks)
        ordered = {spec.name: fields[spec.name] for spec in settings.fields}
        counts = Counter(checks[name].status for name in ordered)
        usage = getattr(self.ctx.llm, "usage", None)
        record = BidRecord(
            bid_id=state["bid_id"],
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            fields=ordered,
            addendum_changes=state.get("addendum_changes", []),
            validation={status: counts.get(status, 0) for status in ("passed", "failed", "not_found")},
            field_validation={name: checks[name] for name in ordered},
            run_info={
                "run_id": state["run_id"],
                "models": {"strong": settings.llm_model, "fast": settings.llm_model_fast},
                "llm_usage": usage.as_dict() if usage else None,
                "duration_s": round(time.time() - state.get("started_at", time.time()), 1),
                "extraction_rounds": max(state.get("attempts", {}).values(), default=0),
                "retried_fields": sorted(f for f, n in state.get("attempts", {}).items() if n > 1),
                "files_indexed": state["ingest_report"].files_indexed if state.get("ingest_report") else [],
                "errors": state.get("errors", []),
            },
        )
        save_record(record, settings)
        return {"final_output": record}

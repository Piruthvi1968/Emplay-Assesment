"""Q&A / Report agent: cited answers from numbered evidence (the extraction report is added in step 7)."""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.agents.evidence import number_evidence, to_source
from app.agents.prompts import ANSWER_PROMPT, ANSWER_SYSTEM
from app.llm_client import LLM, LLMError
from app.models import AnswerPart, QAResponse, QueryPlan, SearchHit, Source, SubEvidence

NOT_FOUND = "Not found in documents"
_MARKER = re.compile(r"\[(\d+)\]")


class CitedQuote(BaseModel):
    evidence: int
    quote: str


class DraftPart(BaseModel):
    sub_question: str
    answer: str | None
    citations: list[CitedQuote]


class DraftAnswer(BaseModel):
    """What the LLM returns; numbers refer to the evidence blocks in the prompt."""

    answer: str
    parts: list[DraftPart]


def answer_question(llm: LLM, question: str, plan: QueryPlan, evidence: list[SubEvidence], run_id: str) -> QAResponse:
    """Answer from evidence only; citation numbers are mapped back to files/pages by code."""
    if plan.intent in ("chitchat", "out_of_scope"):
        return QAResponse(question=question, intent=plan.intent, answer=plan.reply or "", run_id=run_id)
    if not any(sub.hits for sub in evidence):
        return QAResponse(
            question=question,
            intent=plan.intent,
            answer=NOT_FOUND,
            run_id=run_id,
            parts=[AnswerPart(sub_question=s.question) for s in evidence],
        )

    text, by_number = number_evidence(evidence)
    prompt = ANSWER_PROMPT.format(question=question, evidence=text)
    try:
        draft = llm.structured(prompt, DraftAnswer, system=ANSWER_SYSTEM, role="strong", agent="qa_answer")
    except LLMError as exc:
        return QAResponse(
            question=question, intent=plan.intent, run_id=run_id, answer=f"The answer could not be generated ({exc})."
        )
    return _resolve(draft, question, plan.intent, by_number, run_id)


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

"""Extraction agents: one specialist per field group fills its fields from numbered evidence only."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.base import AgentContext, agent_node
from app.agents.evidence import NOT_FOUND, CitedQuote, locate, number_evidence
from app.agents.prompts import EXTRACTION_PROMPT, EXTRACTION_SYSTEM
from app.config import FieldSpec, FieldType
from app.llm_client import LLMError
from app.models import FieldResult, FieldValue, SearchHit, Source, SubEvidence


class FieldDraft(BaseModel):
    field: str
    value: str | list[str] | None
    citations: list[CitedQuote]
    confidence: float
    notes: str


class GroupDraft(BaseModel):
    """What one extraction specialist returns; citation numbers refer to the evidence in its prompt."""

    fields: list[FieldDraft]


def normalize_value(value: FieldValue, field_type: FieldType) -> FieldValue:
    """Lists for list fields, a string for everything else, None for empty or 'not found' answers."""
    if isinstance(value, list):
        items = [v.strip() for v in value if v and v.strip()]
        value = items if field_type == "list" else "; ".join(items)
    elif isinstance(value, str):
        value = value.strip()
        if field_type == "list" and value:
            value = [value]
    if not value or (isinstance(value, str) and value.lower().startswith("not found")):
        return None
    return value


def describe_field(spec: FieldSpec, retry_reason: str | None = None) -> str:
    fmt = f"; format {spec.format}" if spec.format else ""
    line = f"- {spec.name} ({spec.type}{fmt}): {spec.question}"
    return line + (f"\n  Previous answer rejected because: {retry_reason}" if retry_reason else "")


def to_sources(citations: list[CitedQuote], by_number: dict[int, SearchHit]) -> tuple[list[Source], list[str]]:
    """Map cited numbers to sources; numbers that were not in the prompt are reported as issues."""
    sources: list[Source] = []
    issues: list[str] = []
    for c in citations:
        hit = locate(c, by_number)
        if hit is None:
            issues.append(f"cited evidence [{c.evidence}], which was not provided")
            continue
        m = hit.chunk.meta
        quote = c.quote.strip()
        if all((s.chunk_id, s.quote) != (m.chunk_id, quote) for s in sources):  # one source per distinct quote
            sources.append(
                Source(file=m.file_name, page=m.page_number, section=m.section, chunk_id=m.chunk_id, quote=quote)
            )
    return sources, issues


class ExtractionAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def _failed(self, state: dict, exc: Exception) -> dict:
        reason = f"extraction failed: {exc}"
        return {
            "drafts": {f: FieldResult(notes=reason) for f in state["fields"]},
            "extraction_issues": {f: [reason] for f in state["fields"]},
        }

    @agent_node("extraction", on_error=_failed)
    def __call__(self, payload: dict) -> dict:
        """payload (from the orchestrator's Send): group, fields, bid_id, evidence per field, retry_reasons."""
        specs = [self.ctx.specs[name] for name in payload["fields"]]
        reasons = payload.get("retry_reasons", {})
        openings = SubEvidence(
            question="opening pages of the main documents (shared context)", hits=payload.get("openings", [])
        )
        evidence = [openings] + [
            SubEvidence(question=f"for field {s.name}", hits=payload["evidence"].get(s.name, [])) for s in specs
        ]
        text, by_number = number_evidence(evidence, label="Evidence")
        prompt = EXTRACTION_PROMPT.format(
            bid_id=payload["bid_id"],
            group=self.ctx.settings.groups.get(payload["group"], payload["group"]),
            fields="\n".join(describe_field(s, reasons.get(s.name)) for s in specs),
            evidence=text,
        )
        try:
            draft = self.ctx.llm.structured(
                prompt, GroupDraft, system=EXTRACTION_SYSTEM, role="strong", agent=f"extraction:{payload['group']}"
            )
        except LLMError as exc:
            return self._failed(payload, exc)

        returned = {d.field: d for d in draft.fields}
        drafts: dict[str, FieldResult] = {}
        issues: dict[str, list[str]] = {}
        for spec in specs:
            d = returned.get(spec.name)
            if d is None:
                drafts[spec.name] = FieldResult(notes="The extractor returned no answer for this field")
                issues[spec.name] = ["field missing from the extractor's output"]
                continue
            sources, issues[spec.name] = to_sources(d.citations, by_number)
            value = normalize_value(d.value, spec.type)
            notes = d.notes.strip() or (NOT_FOUND if value is None else "")
            drafts[spec.name] = FieldResult(
                value=value,
                sources=sources if value is not None else [],
                confidence=max(0.0, min(1.0, d.confidence)),
                notes=notes,
            )
        return {"drafts": drafts, "extraction_issues": issues}

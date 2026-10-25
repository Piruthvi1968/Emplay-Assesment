"""Shared Pydantic models passed between ingestion, search, agents, and the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DocType = Literal["bid_page", "rfp", "addendum", "specs", "affidavit", "form", "other"]
FieldValue = str | list[str] | None


class Block(BaseModel):
    """A piece of a parsed page: running text or one table (already rendered as Markdown/lines)."""

    kind: Literal["text", "table"]
    text: str
    heading: str | None = None  # section heading known from the source (HTML); PDFs detect it later
    in_margin: bool = False  # PDF text from the top/bottom page margin (header/footer candidates)


class ParsedPage(BaseModel):
    page_number: int | None  # physical page, 1-based; None for HTML
    printed_page: str | None = None
    blocks: list[Block] = Field(default_factory=list)
    empty_or_scanned: bool = False


class ParsedDoc(BaseModel):
    file_name: str
    pages: list[ParsedPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ChunkMeta(BaseModel):
    """Metadata stored with every chunk; used for filtering and citations."""

    chunk_id: str
    bid_id: str
    file_name: str
    doc_type: DocType
    addendum_number: int | None = None
    page_number: int | None = None  # physical PDF page, 1-based; None for HTML
    printed_page: str | None = None  # page label printed on the page ("Page 3 of 40")
    section: str | None = None
    doc_date: str | None = None  # ISO date if detected
    is_table: bool = False
    char_len: int = 0

    def to_chroma(self) -> dict[str, str | int | float | bool]:
        """Chroma rejects None values: store missing ints as -1 and missing strings as ""."""
        out: dict[str, str | int | float | bool] = {}
        for key, value in self.model_dump().items():
            if value is None:
                out[key] = -1 if key in _INT_FIELDS else ""
            else:
                out[key] = value
        return out

    @classmethod
    def from_chroma(cls, meta: dict[str, Any]) -> ChunkMeta:
        """Inverse of `to_chroma`: turn -1 / "" back into None."""
        clean = dict(meta)
        for key in _INT_FIELDS:
            if clean.get(key) == -1:
                clean[key] = None
        for key in _OPTIONAL_STR_FIELDS:
            if clean.get(key) == "":
                clean[key] = None
        return cls.model_validate(clean)


_INT_FIELDS = ("addendum_number", "page_number")
_OPTIONAL_STR_FIELDS = ("printed_page", "section", "doc_date")


class Chunk(BaseModel):
    meta: ChunkMeta
    text: str


class IngestReport(BaseModel):
    bid_id: str
    files_indexed: list[str] = Field(default_factory=list)
    files_skipped: list[str] = Field(default_factory=list)
    files_failed: list[str] = Field(default_factory=list)
    chunks_added: int = 0
    warnings: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    chunk: Chunk
    rrf_score: float = 0.0
    rerank_score: float | None = None
    rank: int = 0


class SubQuery(BaseModel):
    question: str  # standalone question, also used by the re-ranker
    bid_ids: list[str] = Field(default_factory=list)  # [] = all bids
    queries: list[str] = Field(default_factory=list)  # rewritten search phrasings
    keywords: list[str] = Field(default_factory=list)  # exact IDs / codes from the user's text


class QueryPlan(BaseModel):
    intent: Literal["bid_question", "compare", "chitchat", "out_of_scope"]
    sub_queries: list[SubQuery] = Field(default_factory=list)
    reply: str | None = None  # only for chitchat / out_of_scope


class SubEvidence(BaseModel):
    question: str
    hits: list[SearchHit] = Field(default_factory=list)
    no_evidence: bool = False


class Source(BaseModel):
    file: str
    page: int | None = None
    section: str | None = None
    chunk_id: str
    quote: str


class FieldResult(BaseModel):
    value: FieldValue = None
    sources: list[Source] = Field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""


class AddendumChange(BaseModel):
    field: str
    change_type: Literal["override", "clarification"]
    old_value: FieldValue = None
    new_value: FieldValue = None
    addendum: str  # e.g. "Addendum 2"
    source: Source


class FieldValidation(BaseModel):
    status: Literal["passed", "failed", "not_found"]
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)  # failed checks / warnings, human readable


class BidRecord(BaseModel):
    bid_id: str
    generated_at: str
    fields: dict[str, FieldResult]  # the fields.yaml names, in order
    addendum_changes: list[AddendumChange] = Field(default_factory=list)
    validation: dict[str, int] = Field(default_factory=dict)  # passed / failed / not_found counts
    run_info: dict[str, Any] = Field(default_factory=dict)


class AnswerPart(BaseModel):
    sub_question: str
    answer: str | None = None  # None -> "Not found in documents"
    citations: list[Source] = Field(default_factory=list)


class QAResponse(BaseModel):
    question: str
    intent: str
    answer: str
    parts: list[AnswerPart] = Field(default_factory=list)
    citations: list[Source] = Field(default_factory=list)
    run_id: str

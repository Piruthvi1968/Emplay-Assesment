"""Evidence formatting shared by the answer and extraction agents.

The LLM sees evidence as numbered blocks and cites numbers; code maps the numbers back to full
citations, so a model can never invent a file name or page.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from app.ingestion.chunker import doc_label
from app.models import Chunk, SearchHit, Source, SubEvidence
from app.search.engine import chunk_body

_QUOTE_MIN_SIMILARITY = 90  # rapidfuzz partial_ratio; tolerates whitespace/punctuation noise


def evidence_header(chunk: Chunk) -> str:
    m = chunk.meta
    where = f"page {m.page_number}" if m.page_number is not None else f"section {m.section or 'n/a'}"
    return f"{m.bid_id} | {m.file_name} | {doc_label(m.doc_type, m.addendum_number)} | {where}"


def number_evidence(evidence: list[SubEvidence]) -> tuple[str, dict[int, SearchHit]]:
    """Render evidence as '[n] header + text' blocks, grouped by sub-question; a chunk gets one number."""
    lines: list[str] = []
    numbers: dict[str, int] = {}
    by_number: dict[int, SearchHit] = {}
    for i, sub in enumerate(evidence, start=1):
        lines.append(f"## Sub-question {i}: {sub.question}")
        if not sub.hits:
            lines.append("(no evidence found)")
        elif sub.no_evidence:
            lines.append("(low relevance scores: these passages may not answer this sub-question)")
        for hit in sub.hits:
            cid = hit.chunk.meta.chunk_id
            if cid in numbers:
                lines.append(f"(see [{numbers[cid]}])")
                continue
            n = numbers[cid] = len(numbers) + 1
            by_number[n] = hit
            lines.append(f"[{n}] {evidence_header(hit.chunk)}\n{chunk_body(hit.chunk)}")
        lines.append("")
    return "\n".join(lines).strip(), by_number


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quote_supported(quote: str, text: str) -> bool:
    """True if the quote appears in the text (exactly after whitespace normalisation, or near-exactly)."""
    q, t = _normalize(quote), _normalize(text)
    return bool(q) and (q in t or fuzz.partial_ratio(q, t) >= _QUOTE_MIN_SIMILARITY)


def to_source(chunk: Chunk, quote: str) -> Source:
    """A citation for `chunk`. An unsupported quote is replaced by the start of the chunk text."""
    body = chunk_body(chunk)
    if not quote_supported(quote, body):
        quote = body[:300]
    m = chunk.meta
    return Source(file=m.file_name, page=m.page_number, section=m.section, chunk_id=m.chunk_id, quote=quote.strip())

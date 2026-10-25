"""Label documents with generic rules: doc_type, addendum number, and document date.

Every rule here is a keyword or regex that works for any bid folder. Nothing is bid-specific.
"""
from __future__ import annotations

import re
from pathlib import Path

from dateutil import parser as date_parser

from app.models import DocType

# Checked in this order; the first match wins. Addendum comes first because addendum
# file names usually also contain "RFP".
_TYPE_RULES: list[tuple[DocType, re.Pattern[str]]] = [
    ("addendum", re.compile(r"\baddend(um|a)\b|\bamendment\b", re.I)),
    ("affidavit", re.compile(r"affidavit", re.I)),
    ("specs", re.compile(r"\bspec(s|ifications?)?\b|\bquote\b|datasheet", re.I)),
    ("rfp", re.compile(r"\b(rfp|rfq|ifb|porfp|solicitation|request\s+for\s+(proposals?|quotes?|bids?))\b", re.I)),
    ("form", re.compile(r"\bforms?\b|\battachment\b|\bexhibit\b", re.I)),
]

_ADDENDUM_NO = re.compile(r"addendum\s*(?:no\.?|number|#)?\s*(\d+)", re.I)

_DATE_PATTERNS = [
    r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b",  # 27-JUN-2024
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",  # 06/10/2024
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s*\d{4}\b",  # July 9, 2024
]
_DATE_ANY = re.compile("|".join(_DATE_PATTERNS))
# A date right after one of these labels is the document's own date (e.g. BidNet "Publication").
_DATE_LABELLED = re.compile(
    r"(?:publication|issue\s*date|date\s*issued|issued|posted)[^\d]{0,40}?(" + "|".join(_DATE_PATTERNS) + ")",
    re.I,
)

_TOP_CHARS = 600  # how much of the first page counts as "the top"


def detect_doc_type(file_name: str, first_page_text: str) -> DocType | None:
    """Return the doc_type, or None when no rule matches (the caller applies the folder fallback)."""
    if Path(file_name).suffix.lower() in {".html", ".htm"}:
        return "bid_page"
    stem = Path(file_name).stem.replace("_", " ")
    top = first_page_text[:_TOP_CHARS]
    for doc_type, pattern in _TYPE_RULES:
        if pattern.search(stem):
            return doc_type
    for doc_type, pattern in _TYPE_RULES:
        if pattern.search(top):
            return doc_type
    return None


def detect_addendum_number(file_name: str, first_page_text: str) -> int | None:
    """Addendum number from the file name, else from the top of the first page."""
    for text in (Path(file_name).stem.replace("_", " "), first_page_text[:_TOP_CHARS]):
        m = _ADDENDUM_NO.search(text)
        if m:
            return int(m.group(1))
    return None


def detect_doc_date(first_page_text: str) -> str | None:
    """ISO date (YYYY-MM-DD) of the document: a labelled date if present, else the first date near the top."""
    m = _DATE_LABELLED.search(first_page_text)
    candidate = m.group(1) if m else None
    if candidate is None:
        m = _DATE_ANY.search(first_page_text[:_TOP_CHARS * 3])
        candidate = m.group(0) if m else None
    if candidate is None:
        return None
    try:
        return date_parser.parse(candidate).date().isoformat()
    except (ValueError, OverflowError):
        return None


def assign_doc_types(detected: dict[str, DocType | None], sizes: dict[str, int]) -> dict[str, DocType]:
    """Fill in unmatched files: the largest PDF becomes "rfp" if the folder has none; the rest are "other"."""
    out: dict[str, DocType] = {}
    has_rfp = any(t == "rfp" for t in detected.values())
    unmatched_pdfs = [f for f, t in detected.items() if t is None and f.lower().endswith(".pdf")]
    largest = max(unmatched_pdfs, key=lambda f: sizes.get(f, 0)) if unmatched_pdfs and not has_rfp else None
    for f, t in detected.items():
        out[f] = t if t is not None else ("rfp" if f == largest else "other")
    return out

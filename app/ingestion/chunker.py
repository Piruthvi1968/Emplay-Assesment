"""Heading-aware chunking: one requirement / Q&A item per chunk where possible, tables kept whole.

Chunks never cross a page boundary, so every chunk cites exactly one physical page.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import IngestionCfg
from app.ingestion.cleaning import is_heading
from app.models import Chunk, ChunkMeta, DocType, ParsedDoc

_UNIT_START = re.compile(r"^(\d{1,3}[.)]|Q\s*\d{1,3}[.:)]?)\s+\S", re.I)  # numbered item / question
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")
_MIN_BODY_CHARS = 15


def estimate_tokens(text: str) -> int:
    """Rough token count (~4 characters per token for English)."""
    return max(1, len(text) // 4)


def doc_label(doc_type: DocType, addendum_number: int | None) -> str:
    if doc_type == "addendum":
        return f"Addendum {addendum_number}" if addendum_number is not None else "Addendum"
    return {"bid_page": "Bid page", "rfp": "RFP", "specs": "Specs", "affidavit": "Affidavit",
            "form": "Form", "other": "Document"}[doc_type]


def _units(text: str) -> list[tuple[str, str | None]]:
    """Split text into (unit, heading_or_None) pieces: paragraphs, numbered items, and headings."""
    out: list[tuple[str, str | None]] = []
    for para in re.split(r"\n\s*\n", text):
        current: list[str] = []
        for line in para.splitlines():
            line = line.strip()
            if not line:
                continue
            if is_heading(line):
                if current:
                    out.append(("\n".join(current), None))
                    current = []
                out.append((line, line))
            elif _UNIT_START.match(line) and current:
                out.append(("\n".join(current), None))
                current = [line]
            else:
                current.append(line)
        if current:
            out.append(("\n".join(current), None))
    return out


def _split_long(unit: str, max_tokens: int) -> list[str]:
    """Split an oversized unit at sentence boundaries (or hard-cut a single huge sentence)."""
    pieces, buf = [], ""
    for sent in _SENTENCE_SPLIT.split(unit):
        while estimate_tokens(sent) > max_tokens:  # one giant sentence: hard cut on a space
            cut = sent.rfind(" ", 0, max_tokens * 4) if " " in sent[: max_tokens * 4] else max_tokens * 4
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if buf and estimate_tokens(buf + " " + sent) > max_tokens:
            pieces.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        pieces.append(buf)
    return pieces


def _tail(text: str, overlap_tokens: int) -> str:
    """Last ~overlap_tokens of text, starting at a sentence (or word) boundary."""
    if overlap_tokens <= 0 or estimate_tokens(text) <= overlap_tokens:
        return ""
    tail = text[-overlap_tokens * 4:]
    m = _SENTENCE_SPLIT.search(tail)
    if m:
        return tail[m.end():]
    space = tail.find(" ")
    return tail[space + 1:] if space >= 0 else tail


def pack_units(units: list[str], target: int, max_tokens: int, overlap: int) -> list[str]:
    """Greedily pack units into chunks of about `target` tokens; consecutive chunks share an overlap tail."""
    pieces: list[str] = []
    for u in units:
        pieces.extend(_split_long(u, max_tokens) if estimate_tokens(u) > max_tokens else [u])

    chunks: list[str] = []
    buf: list[str] = []
    has_new = False  # buf holds more than the carried-over overlap
    for piece in pieces:
        candidate = "\n".join(buf + [piece])
        if has_new and estimate_tokens(candidate) > target:
            chunks.append("\n".join(buf))
            tail = _tail(chunks[-1], overlap)
            buf = [tail] if tail and estimate_tokens(tail + piece) <= max_tokens else []
        buf.append(piece)
        has_new = True
    if has_new:
        chunks.append("\n".join(buf))
    return chunks


def split_table(text: str, max_tokens: int) -> list[str]:
    """Split a table by rows; a Markdown header (header + separator rows) is repeated in every piece."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    lines = text.splitlines()
    has_header = len(lines) >= 2 and re.fullmatch(r"\|(-+\|)+", lines[1].strip()) is not None
    header = lines[:2] if has_header else []
    rows = lines[2:] if has_header else lines
    pieces, buf = [], list(header)
    for row in rows:
        if len(buf) > len(header) and estimate_tokens("\n".join(buf + [row])) > max_tokens:
            pieces.append("\n".join(buf))
            buf = list(header)
        buf.append(row)
    if len(buf) > len(header):
        pieces.append("\n".join(buf))
    return pieces


def chunk_doc(
    doc: ParsedDoc,
    *,
    bid_id: str,
    doc_type: DocType,
    addendum_number: int | None,
    doc_date: str | None,
    cfg: IngestionCfg,
) -> list[Chunk]:
    """Turn a cleaned ParsedDoc into Chunks with metadata and a context prefix line."""
    stem = Path(doc.file_name).stem
    label = doc_label(doc_type, addendum_number)
    chunks: list[Chunk] = []
    section: str | None = None  # carries over page breaks in PDFs

    for page in doc.pages:
        n = 0
        page_part = f"p{page.page_number}" if page.page_number is not None else "p0"
        for block in page.blocks:
            if block.heading is not None:
                section = block.heading
            bodies: list[tuple[str, str | None, bool]] = []  # (text, section, is_table)
            if block.kind == "table":
                bodies = [(t, section, True) for t in split_table(block.text, cfg.chunk_max_tokens)]
            else:
                # A new heading closes the current section only once that section has some substance,
                # so heading-only or one-line sections merge forward instead of becoming tiny chunks.
                min_section = cfg.chunk_target_tokens // 4
                group: list[str] = []
                group_section = section
                for unit, heading in _units(block.text):
                    if heading is not None and group and estimate_tokens("\n".join(group)) >= min_section:
                        bodies += [(t, group_section, False) for t in pack_units(
                            group, cfg.chunk_target_tokens, cfg.chunk_max_tokens, cfg.chunk_overlap_tokens)]
                        group = []
                    if heading is not None:
                        section = heading
                        if not group:
                            group_section = heading
                    group.append(unit)
                if group:
                    bodies += [(t, group_section, False) for t in pack_units(
                        group, cfg.chunk_target_tokens, cfg.chunk_max_tokens, cfg.chunk_overlap_tokens)]

            for body, sec, is_table in bodies:
                if len(body.strip()) < _MIN_BODY_CHARS:
                    continue
                prefix_parts = [bid_id, label, stem]
                if page.page_number is not None:
                    prefix_parts.append(f"p.{page.page_number}")
                if sec:
                    prefix_parts.append(f"Section: {sec}")
                text = "[" + " | ".join(prefix_parts) + "]\n" + body
                meta = ChunkMeta(
                    chunk_id=f"{bid_id}::{stem}::{page_part}::c{n}",
                    bid_id=bid_id,
                    file_name=doc.file_name,
                    doc_type=doc_type,
                    addendum_number=addendum_number,
                    page_number=page.page_number,
                    printed_page=page.printed_page,
                    section=sec[:200] if sec else None,
                    doc_date=doc_date,
                    is_table=is_table,
                    char_len=len(text),
                )
                chunks.append(Chunk(meta=meta, text=text))
                n += 1
    return chunks

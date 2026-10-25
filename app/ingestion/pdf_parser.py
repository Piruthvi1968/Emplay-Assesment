"""PDF -> ParsedDoc with PyMuPDF: page text blocks plus tables, one ParsedPage per physical page."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pymupdf

from app.models import Block, ParsedDoc, ParsedPage

log = logging.getLogger(__name__)

_MARGIN = 0.12  # top/bottom fraction of the page treated as header/footer area
_SMALL_TABLE_CHARS = 400  # tables up to ~100 tokens stay inside the surrounding text
_PRINTED_PAGE = re.compile(r"\bpage\s+(\d+)\s*(?:of|\|)\s*(\d+)\b", re.I)


def table_to_markdown(rows: list[list[str | None]]) -> str | None:
    """Render a data grid as a Markdown table, or return None for a layout table.

    A data grid has a real header row: every cell filled and all different (price schedules,
    SKU lists). Forms often use tables only for positioning; their cells are merged and repeated,
    and PyMuPDF's cell text is unreliable for justified text (it can drop dots in e-mail addresses),
    so for those the caller keeps the normal page text instead. Merged cells come back repeated in
    every column they span, so consecutive duplicates are collapsed before the header check.
    """
    clean: list[list[str]] = []
    for row in rows:
        cells = [re.sub(r"\s+", " ", c).strip() if c else "" for c in row]
        collapsed: list[str] = []
        for c in cells:
            if collapsed and c and c == collapsed[-1]:
                continue
            collapsed.append(c)
        clean.append(collapsed)

    header = clean[0] if clean else []
    is_grid = (
        len(clean) >= 2
        and len(header) >= 2
        and all(header)
        and len(set(header)) == len(header)
        and all(len(r) == len(header) for r in clean)
    )
    if not is_grid:
        return None
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:] if any(r)]
    return "\n".join(lines)


def table_rows_from_words(table: pymupdf.table.Table, words: list[tuple], used: set[int]) -> list[list[str | None]]:
    """Cell text built from the page's words (each word goes to the cell holding its centre).

    PyMuPDF's own `table.extract()` can drop spaces and move hyphens ('210-BLYZ' -> '210BLYZ -'),
    which breaks exact-ID search. Words keep the original spelling. `used` stops a word from being
    counted twice when cell boxes overlap.
    """
    rows: list[list[str | None]] = []
    for row in table.rows:
        cells: list[str | None] = []
        for bbox in row.cells:
            if bbox is None:
                cells.append(None)
                continue
            rect, picked = pymupdf.Rect(bbox), []
            for i, w in enumerate(words):
                if i not in used and pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) in rect:
                    picked.append(w[4])
                    used.add(i)
            cells.append(" ".join(picked))
        rows.append(cells)
    return rows


def _inside(block_rect: pymupdf.Rect, table_rects: list[pymupdf.Rect]) -> bool:
    """A text block belongs to a table if its centre lies in the table's bbox."""
    centre = pymupdf.Point((block_rect.x0 + block_rect.x1) / 2, (block_rect.y0 + block_rect.y1) / 2)
    return any(centre in r for r in table_rects)


def body_blocks(body: list[tuple[float, str]], grids: list[tuple[pymupdf.Rect, str]]) -> list[Block]:
    """Body text in native order with each table placed where it sits on the page.

    A table goes before the first text block that starts below it, so it gets the section of the
    heading above it. Small tables (e.g. a two-row form grid) stay inside the surrounding text instead
    of becoming tiny chunks of their own.
    """
    blocks: list[Block] = []
    parts: list[str] = []
    pending = sorted(grids, key=lambda grid: grid[0].y0)

    def place(markdown: str) -> None:
        if len(markdown) <= _SMALL_TABLE_CHARS:
            parts.append(markdown)
            return
        if parts:
            blocks.append(Block(kind="text", text="\n\n".join(parts)))
            parts.clear()
        blocks.append(Block(kind="table", text=markdown))

    for y0, text in body:
        while pending and pending[0][0].y0 <= y0:
            place(pending.pop(0)[1])
        parts.append(text)
    for _, markdown in pending:
        place(markdown)
    if parts:
        blocks.append(Block(kind="text", text="\n\n".join(parts)))
    return blocks


def parse_pdf(path: Path, min_chars_per_page: int = 30) -> ParsedDoc:
    """Parse every page. Table areas are removed from the running text so content is not duplicated."""
    doc = ParsedDoc(file_name=path.name)
    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            page_no = index + 1
            try:
                tables = page.find_tables().tables
            except Exception as exc:  # table detection is best effort; keep the page text
                log.warning("table detection failed on %s p.%d: %s", path.name, page_no, exc)
                tables = []
            grids: list[tuple[pymupdf.Rect, str]] = []
            words = page.get_text("words") if tables else []
            used: set[int] = set()
            for t in tables:
                rendered = table_to_markdown(table_rows_from_words(t, words, used))
                if rendered:
                    grids.append((pymupdf.Rect(t.bbox), rendered))
            grid_rects = [rect for rect, _ in grids]

            # Native block order follows the authoring order (labels next to values in forms), but
            # headers/footers can sit anywhere in it, so margin blocks are split out by position and
            # marked; cleaning.remove_repeated_lines only removes lines from those.
            height = page.rect.height
            top: list[str] = []
            bottom: list[str] = []
            body: list[tuple[float, str]] = []  # (top y, text) in native order
            for b in page.get_text("blocks"):
                if b[6] != 0 or not b[4].strip() or _inside(pymupdf.Rect(b[:4]), grid_rects):
                    continue
                if b[3] <= height * _MARGIN:
                    top.append(b[4].strip())
                elif b[1] >= height * (1 - _MARGIN):
                    bottom.append(b[4].strip())
                else:
                    body.append((b[1], b[4].strip()))
            blocks = [Block(kind="text", text="\n\n".join(top), in_margin=True)] if top else []
            blocks += body_blocks(body, grids)
            if bottom:
                blocks.append(Block(kind="text", text="\n\n".join(bottom), in_margin=True))

            full = page.get_text("text")
            m = _PRINTED_PAGE.search(full)
            parsed = ParsedPage(
                page_number=page_no,
                printed_page=f"{m.group(1)} of {m.group(2)}" if m else None,
                blocks=blocks,
            )
            if len(full.strip()) < min_chars_per_page and not tables:
                parsed.empty_or_scanned = True
                doc.warnings.append(f"{path.name} p.{page_no}: empty or scanned page (no text layer)")
            doc.pages.append(parsed)
    return doc


def first_page_text(path: Path, max_pages: int = 2) -> str:
    """Raw text of the first page(s), for cheap labelling without a full parse."""
    with pymupdf.open(path) as pdf:
        return "\n".join(pdf[i].get_text("text") for i in range(min(max_pages, pdf.page_count)))

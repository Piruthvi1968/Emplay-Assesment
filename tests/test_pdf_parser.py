from pathlib import Path

import pymupdf

from app.ingestion.cleaning import clean_doc
from app.ingestion.pdf_parser import parse_pdf, table_to_markdown
from tests.conftest import make_pdf


def test_pages_tables_and_empty_flag(tmp_path: Path) -> None:
    pdf = make_pdf(
        tmp_path / "rfp.pdf",
        ["1.1 Scope\nThe vendor shall deliver 40 tablets.", "Second page text about warranty terms.", ""],
        table_rows=[["SKU", "Description"], ["210-BLYZ", "Base laptop"], ["379-BFNZ", "Processor"]],
    )
    doc = parse_pdf(pdf, min_chars_per_page=30)
    assert [p.page_number for p in doc.pages] == [1, 2, 3]
    assert doc.pages[0].printed_page == "1 of 3"

    tables = [b for b in doc.pages[0].blocks if b.kind == "table"]
    assert len(tables) == 1
    assert "| SKU | Description |" in tables[0].text
    assert "210-BLYZ" in tables[0].text  # hyphenated IDs survive intact
    text = next(b for b in doc.pages[0].blocks if b.kind == "text").text
    assert "210-BLYZ" not in text  # table area is not duplicated in the running text

    assert not any(p.empty_or_scanned for p in doc.pages)


def test_blank_page_is_flagged(tmp_path: Path) -> None:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((50, 100), "A page with a normal text layer on it.", fontsize=11)
    pdf.new_page()  # no text layer, like a scanned image page
    pdf.save(tmp_path / "scan.pdf")
    doc = parse_pdf(tmp_path / "scan.pdf", min_chars_per_page=30)
    assert [p.empty_or_scanned for p in doc.pages] == [False, True]
    assert doc.warnings == ["scan.pdf p.2: empty or scanned page (no text layer)"]


def test_repeated_header_footer_removed_except_first(tmp_path: Path) -> None:
    pages = [f"Body text of page {i} with enough characters to count." for i in range(1, 5)]
    doc = clean_doc(parse_pdf(make_pdf(tmp_path / "r.pdf", pages)), min_repeat=0.5)
    texts = ["\n".join(b.text for b in p.blocks) for p in doc.pages]
    assert "Header Line" in texts[0]
    assert all("Header Line" not in t for t in texts[1:])
    assert all("Page " not in t for t in texts)  # bare page labels are dropped
    assert "Body text of page 3" in texts[2]


def test_layout_table_is_not_a_grid() -> None:
    merged = [["Section 1", "Section 1", "Section 1"], ["Name:", "", "Jane Doe"]]
    assert table_to_markdown(merged) is None
    grid = [["Line", "Item", "Qty"], ["1", "Laptop", "30"]]
    assert table_to_markdown(grid).splitlines()[0] == "| Line | Item | Qty |"

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

    page_text = "\n".join(b.text for b in doc.pages[0].blocks)
    assert "| SKU | Description |\n|---|---|\n| 210-BLYZ | Base laptop |" in page_text  # IDs intact
    assert page_text.count("210-BLYZ") == 1  # the table area is not duplicated in the running text

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


def test_tables_sit_where_they_are_on_the_page() -> None:
    from app.ingestion.pdf_parser import body_blocks

    small = "| Description | Response |\n|---|---|\n| Make and model | |"
    rows = [f"| Laptop model {i} with a long description | {i} |" for i in range(12)]
    big = "| Item | Qty |\n|---|---|\n" + "\n".join(rows)
    body = [(100.0, "2.2.1 Line 1 Laptop"), (300.0, "2.2.2 Line 2 Tablet"), (500.0, "Closing text")]
    grids = [(pymupdf.Rect(0, 400, 500, 450), big), (pymupdf.Rect(0, 200, 500, 250), small)]
    blocks = body_blocks(body, grids)
    assert [b.kind for b in blocks] == ["text", "table", "text"]
    assert blocks[0].text == f"2.2.1 Line 1 Laptop\n\n{small}\n\n2.2.2 Line 2 Tablet"  # small grid stays in its text
    assert blocks[1].text == big and blocks[2].text == "Closing text"  # big grid before the text below it

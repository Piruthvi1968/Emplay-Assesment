from pathlib import Path

from app.ingestion.html_parser import parse_html
from tests.conftest import BIDNET_LIKE_HTML


def test_label_value_pairs_sections_and_boilerplate(tmp_path: Path) -> None:
    f = tmp_path / "listing.html"
    f.write_text(BIDNET_LIKE_HTML, encoding="utf-8")
    doc = parse_html(f)
    assert len(doc.pages) == 1 and doc.pages[0].page_number is None

    blocks = {b.heading: b.text for b in doc.pages[0].blocks}
    assert "Solicitation Number: ABC-123" in blocks["Basic Information"]
    assert "Closing Date: 03/20/2025 02:00 PM EST" in blocks["Dates"]
    assert "| Item | Qty |" in blocks["Dates"]  # the <table> after the Dates block

    everything = "\n".join(blocks.values())
    for junk in ("cookies", "Login", "var x", "Print"):
        assert junk not in everything

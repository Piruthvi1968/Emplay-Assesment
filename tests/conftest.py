"""Shared fixtures: tiny synthetic PDF/HTML files and isolated settings (no network, no real data)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from app.config import Settings, load_settings

BIDNET_LIKE_HTML = """<html><head><title>Bid</title><script>var x=1;</script></head><body>
<nav><a>Home</a><a>Login</a></nav>
<div class="cookie-banner">We use cookies. Allow all cookies</div>
<main>
  <h2>ABC-123 - Rugged Tablets</h2>
  <div class="content-block"><h3>Basic Information</h3>
    <div class="field"><span>Solicitation Number</span><div><p>ABC-123</p></div></div>
    <div class="field"><span>Issuing Organization</span><div><p>Example County Schools</p></div></div>
  </div>
  <div class="content-block"><h3>Dates</h3>
    <div class="field"><span>Publication</span><div><p>03/01/2025 09:00 AM EST</p></div></div>
    <div class="field"><span>Closing Date</span><div><p>03/20/2025 02:00 PM EST</p></div></div>
  </div>
  <span>Print</span><span>Print</span>
  <table><tr><th>Item</th><th>Qty</th></tr><tr><td>Tablet</td><td>40</td></tr></table>
</main></body></html>"""


def make_pdf(path: Path, pages: list[str], table_rows: list[list[str]] | None = None) -> Path:
    """Write a small PDF: one text page per entry; optionally a ruled table on the first page."""
    doc = pymupdf.open()
    for i, text in enumerate(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 40), "Example District RFP 2025-01 Header Line", fontsize=9)
        page.insert_textbox(pymupdf.Rect(50, 120, 545, 420), text, fontsize=10)
        page.insert_text((50, 815), f"Page {i + 1} of {len(pages)}", fontsize=9)
        if i == 0 and table_rows:
            x0, y0, cw, rh = 50, 450, 160, 20
            for r, row in enumerate(table_rows):
                for c, cell in enumerate(row):
                    rect = pymupdf.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
                    page.draw_rect(rect, color=(0, 0, 0), width=0.8)
                    page.insert_text((rect.x0 + 4, rect.y1 - 6), cell, fontsize=9)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Real config, but index/manifest paths redirected into a temp dir."""
    s = load_settings()
    s.paths.index_dir = tmp_path / "index" / "chroma"
    s.paths.manifest = tmp_path / "index" / "manifest.json"
    return s

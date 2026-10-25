"""HTML -> ParsedDoc: strip boilerplate, keep headings, label/value pairs, and tables.

Portal pages such as BidNet are built from <div>s, not <table>s: each field is a small element
holding a short label followed by a value. Those pairs are rewritten as "Label: Value" lines so
the facts stay attached to their labels. All rules are generic (no site-specific class names).
"""
from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from app.models import Block, ParsedDoc, ParsedPage

_REMOVE_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "svg", "button",
                "input", "select", "textarea", "iframe", "link", "meta"]
_BOILERPLATE = re.compile(r"cookie|consent|gdpr|modal|popup|dialog", re.I)
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
_HEADING_MARK = "\u0000H "  # temporary line prefix marking a heading
_MAX_LABEL_CHARS = 40
_LONG_VALUE_CHARS = 200


def _text(el: Tag) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _table_to_markdown(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [_text(c) for c in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _is_pair(el: Tag) -> tuple[str, str] | None:
    """An element with exactly two child elements: a short label, then a non-empty value."""
    if el.name in _HEADINGS:
        return None
    children = [c for c in el.children if isinstance(c, Tag)]
    stray_text = "".join(str(c) for c in el.children if isinstance(c, NavigableString)).strip()
    if len(children) != 2 or stray_text:
        return None
    label, value = _text(children[0]), _text(children[1])
    if not label or not value or len(label) > _MAX_LABEL_CHARS or label.endswith("."):
        return None
    if children[0].find(["p", "div", "table", "ul", "ol"] + _HEADINGS):
        return None
    return label.rstrip(":"), value


def _keep_line(line: str, seen: set[str]) -> bool:
    """Drop repeated lines (menus appear several times) and lone UI words such as 'Print' or 'Cancel'."""
    if line in seen:
        return False
    seen.add(line)
    if line.startswith(_HEADING_MARK) or ": " in line or line.startswith("|"):
        return True
    return len(line.split()) >= 2 or bool(re.search(r"[\d@]", line))


def parse_html(path: Path) -> ParsedDoc:
    """Parse one HTML file into a single ParsedPage (page_number=None) with one block per section."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for tag in soup(_REMOVE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        marker = " ".join(tag.get("class") or []) + " " + str(tag.get("id") or "")
        if _BOILERPLATE.search(marker):
            tag.decompose()

    root = soup.select_one("main, article, [role=main]") or soup.body or soup

    for table in root.find_all("table"):
        table.replace_with(NavigableString("\n" + _table_to_markdown(table) + "\n"))
    for h in root.find_all(_HEADINGS):
        h.replace_with(NavigableString(f"\n{_HEADING_MARK}{_text(h)}\n"))
    # Innermost elements first, so a pair is found at the level of the individual field.
    for el in reversed(root.find_all(True)):
        if el.decomposed or el.parent is None:
            continue
        pair = _is_pair(el)
        if pair and len(pair[1]) > _LONG_VALUE_CHARS:  # a long free-text field is a section of its own
            el.replace_with(NavigableString(f"\n{_HEADING_MARK}{pair[0]}\n{pair[1]}\n"))
        elif pair:
            el.replace_with(NavigableString(f"\n{pair[0]}: {pair[1]}\n"))

    seen: set[str] = set()
    lines = [ln.strip() for ln in root.get_text("\n").splitlines()]
    lines = [ln for ln in lines if ln and _keep_line(ln, seen)]

    blocks: list[Block] = []
    heading: str | None = None
    current: list[str] = []
    for ln in lines + [_HEADING_MARK]:  # sentinel flushes the last section
        if ln.startswith(_HEADING_MARK):
            if current:
                blocks.append(Block(kind="text", text="\n".join(current), heading=heading))
            heading, current = ln[len(_HEADING_MARK):].strip() or None, []
        else:
            current.append(ln)

    doc = ParsedDoc(file_name=path.name, pages=[ParsedPage(page_number=None, blocks=blocks)])
    if not blocks:
        doc.warnings.append(f"{path.name}: no text content found")
    return doc

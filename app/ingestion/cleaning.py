"""Text cleaning: repeated header/footer removal, character normalisation, and re-joining broken lines."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

from app.models import Block, ParsedDoc

_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "•": "-",
        "●": "-",
        "▪": "-",
        "": "-",
        "·": "-",
    }
)

_LIST_ITEM = re.compile(r"^(\d{1,3}[.)]|[a-zA-Z][.)]|\([a-zA-Z0-9]{1,3}\)|[-*])\s+")
_LABEL_VALUE = re.compile(r"^[A-Z][\w /&()'-]{1,40}: \S")  # "Closing Date: 07/09/2024 ..." starts its own line
_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(\.\d{1,2})*\.?|section\s+\d+[\w.]*|article\s+[\w.]+)\s+[A-Za-z]", re.I)
_SENTENCE_END = re.compile(r"[.:;?!]\"?$")
_DATE_START = re.compile(
    r"^(\d{1,2}[-/](\d{1,2}|[A-Za-z]{3})[-/]\d{2,4}"  # 27-JUN-2024, 06/10/2024
    r"|(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4})\b"  # July 9, 2024
)
_PAGE_LABEL = re.compile(r"^\s*page\s+\d+\s*(of|\|)\s*\d+\s*$", re.I)


def normalize_chars(text: str) -> str:
    """NFKC (splits ligatures like 'fi'), curly quotes/dashes/bullets to ASCII, tidy spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)
    text = re.sub(r"[ \t]+", " ", text)
    # E-mail addresses wrapped inside narrow cells: "a@b.state.md .us" / "a@b.state.\nmd.us"
    text = re.sub(r"(\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*)\s+(\.[a-z]{2,}\b)", r"\1\2", text)
    text = re.sub(r"(\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.)\s+([a-z]{2,}(?:\.[a-z]{2,})*\b)", r"\1\2", text)
    return re.sub(r" *\n *", "\n", text).strip()


def _line_key(line: str) -> str:
    """Normalise a line for repeat detection: digits vary ('Page 3 of 40'), so they are masked."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", line.lower())).strip()


def is_heading(line: str) -> bool:
    """Short numbered heading ('1.2 Terms', 'Section 4 ...') or short ALL-CAPS line."""
    s = line.strip()
    if not s or len(s) > 80 or s.startswith("|"):
        return False
    # commas, '@', '#' or a trailing ':' point to addresses, names, IDs or form labels, not headings
    if re.search(r"[,@#]|:$", s):
        return False
    if _NUMBERED_HEADING.match(s) and not _SENTENCE_END.search(s) and len(s.split()) <= 10:
        # "1. The vendor shall ..." is a list item, not a heading: headings are short and title-like
        return not re.match(r"^\d{1,3}[.)]\s", s) or len(s.split()) <= 4
    letters = [c for c in s if c.isalpha()]
    upper = sum(c.isupper() for c in letters)
    # all caps, or nearly: 'INTERLOCAL AGREEMENT ("Rider") CONSENT FORM', 'ADDENDUM No. 2'
    caps = (len(letters) >= 4 and upper == len(letters)) or (len(letters) >= 8 and upper >= 0.85 * len(letters))
    return caps and len(s.split()) <= 10


def remove_repeated_lines(doc: ParsedDoc, min_repeat: float) -> int:
    """Remove header/footer lines that repeat on >= min_repeat of the pages.

    Only lines in page-margin blocks (marked by the PDF parser from their position) are candidates,
    and the first occurrence is kept so the document title still appears once.
    Returns the number of removed lines.
    """
    pages = [p for p in doc.pages if p.blocks]
    if len(pages) < 2:
        return 0
    counts: Counter[str] = Counter()
    for p in pages:
        counts.update({_line_key(ln) for b in p.blocks if b.in_margin for ln in b.text.splitlines() if ln.strip()})
    threshold = max(2, math.ceil(min_repeat * len(pages)))
    repeated = {k for k, n in counts.items() if n >= threshold and k}

    removed = 0
    kept_once: set[str] = set()
    for p in pages:
        for b in p.blocks:
            if not b.in_margin:
                continue
            out = []
            for ln in b.text.splitlines():
                key = _line_key(ln)
                if key in repeated:
                    if key in kept_once:
                        removed += 1
                        continue
                    kept_once.add(key)
                out.append(ln)
            b.text = "\n".join(out)
    return removed


def _merge_text_blocks(blocks: list[Block]) -> list[Block]:
    """After header/footer removal, a page's text blocks (margin + body) become one text block."""
    texts = [b for b in blocks if b.kind == "text" and b.text.strip()]
    if len(texts) <= 1 or any(b.heading for b in texts):  # HTML sections keep their own blocks
        return [b for b in blocks if b.text.strip()]
    merged = Block(kind="text", text="\n\n".join(b.text for b in texts))
    return [merged] + [b for b in blocks if b.kind != "text" and b.text.strip()]


def _label_value_pair(prev: str, prev_raw: str, line: str) -> str | None:
    """Forms print a label and its value on separate lines; rejoin them as 'Label: value'.

    'Solicitation Due' + '27-JUN-2024 14:00:00' -> 'Solicitation Due: 27-JUN-2024 14:00:00'
    'Initial Term:' + '3'                      -> 'Initial Term: 3'
    Only an unjoined previous line qualifies, so wrapped sentences are never split into pairs.
    """
    if not prev or prev != prev_raw or is_heading(prev) or _LIST_ITEM.match(line) or is_heading(line):
        return None
    if prev.endswith(":") and len(line.split()) <= 4 and not _LABEL_VALUE.match(line):
        return f"{prev} {line}"
    words = prev.split()
    if _DATE_START.match(line) and len(words) <= 6 and not _SENTENCE_END.search(prev) and not prev[-1].isdigit():
        return f"{prev}: {line}"
    return None


def join_broken_lines(text: str) -> str:
    """Re-join lines that the PDF layout broke, keeping headings, list items and paragraphs apart.

    A line is glued to the previous one when the previous line does not end a sentence and either
    the previous line was long (a wrapped sentence) or both lines are tiny fragments (justified
    form text that PDF extraction emits one word per line). Line-end hyphens are kept
    ('pre-\\nproposal' -> 'pre-proposal'), because in generated RFPs they are usually real hyphens.
    """
    out: list[str] = []
    prev_raw = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if _PAGE_LABEL.match(line):  # bare "Page 3 of 40": already kept as printed_page metadata
            continue
        if not line:
            out.append("")
            prev_raw = ""
            continue
        pair = _label_value_pair(out[-1] if out else "", prev_raw, line)
        if pair:
            out[-1] = pair
            prev_raw = ""  # the pair is complete: never glue the next label onto it
            continue
        can_join = (
            out
            and out[-1]
            and prev_raw
            and not _SENTENCE_END.search(prev_raw)
            and not is_heading(prev_raw)
            and not is_heading(line)
            and not _LIST_ITEM.match(line)
            and not _LABEL_VALUE.match(line)
            and not line.startswith("|")
            and not prev_raw.startswith("|")
            and (len(prev_raw) >= 60 or (len(prev_raw.split()) <= 3 and len(line.split()) <= 3) or line[0].islower())
        )
        if can_join:
            sep = "" if out[-1].endswith("-") else " "
            out[-1] = out[-1] + sep + line
        else:
            out.append(line)
        prev_raw = line
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_doc(doc: ParsedDoc, min_repeat: float) -> ParsedDoc:
    """Normalise characters, drop repeated headers/footers, then re-join broken lines (in place)."""
    for p in doc.pages:
        for b in p.blocks:
            b.text = normalize_chars(b.text)
    remove_repeated_lines(doc, min_repeat)
    for p in doc.pages:
        for b in p.blocks:
            if b.kind == "text":
                b.text = join_broken_lines(b.text)
        p.blocks = _merge_text_blocks(p.blocks)
    return doc

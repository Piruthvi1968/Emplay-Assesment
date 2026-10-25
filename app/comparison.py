"""Side-by-side comparison of extracted bids: one Markdown report built from the saved records (no LLM, no cost)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.models import BidRecord, FieldResult, FieldValidation, Source

_STATUS = {"passed": "✓", "failed": "✗", "not_found": "–"}
_MAX_CELL = 300


def load_records(outputs_dir: Path, bid_ids: list[str] | None = None) -> list[BidRecord]:
    """Saved records from outputs/<bid>.json, all of them or the requested bids (missing ones are skipped)."""
    paths = [outputs_dir / f"{b}.json" for b in bid_ids] if bid_ids else sorted(outputs_dir.glob("*.json"))
    return [BidRecord.model_validate_json(p.read_text(encoding="utf-8")) for p in paths if p.is_file()]


def _cell(text: str) -> str:
    """Markdown-table safe: one line, no pipes, shortened."""
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1].rstrip() + "…"


def _where(src: Source) -> str:
    return f"{src.file} p.{src.page}" if src.page is not None else f"{src.file} ({src.section or 'web page'})"


def _show(value: str | list[str] | None) -> str:
    return "not found" if value is None else "; ".join(value) if isinstance(value, list) else value


def field_cell(result: FieldResult, check: FieldValidation | None) -> str:
    """Value, status, confidence and the first citation of one field of one bid."""
    value = "; ".join(result.value) if isinstance(result.value, list) else (result.value or "Not found in documents")
    status = check.status if check else "failed"
    cited = f" · {_where(result.sources[0])}" if result.sources else ""
    return f"{_cell(value)}<br>{_STATUS[status]} {status} · {result.confidence:.2f}{cited}"


def compare_markdown(records: list[BidRecord]) -> str:
    """The report: an overview row per bid, one table row per field, then each bid's addendum changes."""
    if not records:
        return "# Bid comparison\n\nNo extracted bids found. Run `python main.py extract --all` first.\n"
    names = list(dict.fromkeys(name for r in records for name in r.fields))
    lines = [
        "# Bid comparison",
        "",
        f"Generated {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} from the saved extraction records "
        f"({', '.join(r.bid_id for r in records)}). Each cell: value, validation status, confidence and the first "
        "citation; full sources and notes are in outputs/<bid>.json.",
        "",
        "| Field | " + " | ".join(r.bid_id for r in records) + " |",
        "|---|" + "---|" * len(records),
        "| Validation | "
        + " | ".join(" / ".join(f"{k} {r.validation.get(k, 0)}" for k in _STATUS) for r in records)
        + " |",
    ]
    for name in names:
        cells = [
            field_cell(r.fields[name], r.field_validation.get(name)) if name in r.fields else "(not extracted)"
            for r in records
        ]
        lines.append(f"| **{name}** | " + " | ".join(cells) + " |")
    lines += ["", "## Addendum changes", ""]
    for r in records:
        lines.append(f"**{r.bid_id}:** " + ("none" if not r.addendum_changes else ""))
        for c in r.addendum_changes:
            old, new = _cell(_show(c.old_value)), _cell(_show(c.new_value))
            change = "value unchanged" if c.old_value == c.new_value else f"{old} → {new}"
            lines.append(f"- {c.addendum}, {c.change_type} of *{c.field}*: {change} ({_where(c.source)})")
        lines.append("")
    return "\n".join(lines)


def write_comparison(outputs_dir: Path, bid_ids: list[str] | None = None) -> Path:
    path = outputs_dir / "comparison.md"
    path.write_text(compare_markdown(load_records(outputs_dir, bid_ids)), encoding="utf-8")
    return path

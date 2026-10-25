"""The side-by-side comparison report built from saved records."""

from __future__ import annotations

from pathlib import Path

from app.agents.report_agent import save_record
from app.comparison import compare_markdown, load_records, write_comparison
from app.config import Settings
from app.models import AddendumChange, BidRecord, FieldResult, FieldValidation, Source

SRC = Source(file="Addendum 2.pdf", page=1, chunk_id="c1", quote="The new due date")


def _record(bid_id: str, due: str | None, changes: list[AddendumChange] | None = None) -> BidRecord:
    status = "passed" if due else "not_found"
    return BidRecord(
        bid_id=bid_id,
        generated_at="2026-10-25T10:00:00+00:00",
        fields={"Due Date": FieldResult(value=due, sources=[SRC] if due else [], confidence=0.9)},
        addendum_changes=changes or [],
        validation={status: 1},
        field_validation={"Due Date": FieldValidation(status=status)},
    )


def test_one_row_per_field_and_one_column_per_bid() -> None:
    confirm = AddendumChange(
        field="Due Date",
        change_type="clarification",
        old_value="2024-06-27",
        new_value="2024-06-27",
        addendum="Addendum 1",
        source=SRC,
    )
    report = compare_markdown([_record("Bid1", "2024-07-09 14:00 CST | late", [confirm]), _record("Bid2", None)])
    row = next(line for line in report.splitlines() if line.startswith("| **Due Date**"))
    assert row.count(" | ") == 2  # field, Bid1, Bid2: the value's own "|" was escaped
    assert "2024-07-09 14:00 CST / late<br>✓ passed · 0.90 · Addendum 2.pdf p.1" in row
    assert "Not found in documents<br>– not_found" in row
    assert "Addendum 1, clarification of *Due Date*: value unchanged" in report
    assert "**Bid2:** none" in report


def test_records_are_read_from_outputs(tmp_path: Path, settings: Settings) -> None:
    settings.paths.outputs_dir = tmp_path
    for record in (_record("Bid1", "2024-07-09"), _record("Bid2", None)):
        save_record(record, settings)
    assert [r.bid_id for r in load_records(tmp_path)] == ["Bid1", "Bid2"]
    assert [r.bid_id for r in load_records(tmp_path, ["Bid2", "Bid9"])] == ["Bid2"]  # missing bids are skipped
    assert "| Field | Bid1 | Bid2 |" in write_comparison(tmp_path).read_text(encoding="utf-8")
    assert "No extracted bids" in compare_markdown([])

"""Ingestion agent: parses and indexes the bid folder (incremental: unchanged files are skipped)."""

from __future__ import annotations

from pathlib import Path

from app.agents.base import AgentContext, agent_node
from app.agents.state import BidState


class IngestionAgent:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    @agent_node("ingestion")
    def __call__(self, state: BidState) -> dict:
        report = self.ctx.index_folder(Path(state["bid_folder"]))
        self.ctx.trace.log(
            "ingestion",
            "tool_call",
            tool="ingest_folder",
            indexed=report.files_indexed,
            skipped=report.files_skipped,
            failed=report.files_failed,
            chunks_added=report.chunks_added,
            warnings=report.warnings,
        )
        if not any(c.meta.bid_id == state["bid_id"] for c in self.ctx.engine.chunks()):
            return {"ingest_report": report, "errors": [f"ingestion: no readable documents in {state['bid_folder']}"]}
        return {"ingest_report": report}

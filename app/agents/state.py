"""Shared state of the agent graph: every agent reads from and writes to this one object."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from app.models import (
    AddendumChange,
    BidRecord,
    FieldResult,
    FieldValidation,
    IngestReport,
    QAResponse,
    QueryPlan,
    SearchHit,
    SubEvidence,
)


def merge_dicts(old: dict | None, new: dict | None) -> dict:
    """Reducer: agents running in parallel each return entries for their own keys; LangGraph merges them."""
    return {**(old or {}), **(new or {})}


class BidState(TypedDict, total=False):
    run_id: str
    mode: Literal["extract", "qa"]
    errors: Annotated[list[str], operator.add]  # every agent can append; nothing is lost
    final_output: BidRecord | QAResponse

    # extraction mode
    bid_folder: str
    bid_id: str
    started_at: float
    ingest_report: IngestReport
    fields_todo: list[str]  # fields (re)processed in the current round
    attempts: dict[str, int]  # extraction rounds per field
    rounds: int  # completed validation rounds, counted by the orchestrator (bounds the retry loop)
    retry_reasons: dict[str, str]  # validator's reason, shown to the extractor on the next round
    evidence: Annotated[dict[str, list[SearchHit]], merge_dicts]
    openings: list[SearchHit]  # first chunks of the main documents, shared by all extraction groups
    drafts: Annotated[dict[str, FieldResult], merge_dicts]  # the 3 extraction groups merge here
    extraction_issues: Annotated[dict[str, list[str]], merge_dicts]  # e.g. a cited number that was not provided
    addendum_changes: list[AddendumChange]
    validation: dict[str, FieldValidation]
    history: dict[str, list[tuple[FieldResult, FieldValidation]]]  # every attempt, to keep the best one

    # question-answering mode
    question: str
    plan: QueryPlan
    qa_evidence: list[SubEvidence]
    qa_round: int
    follow_ups: list[str]  # extra searches the answer agent asked for

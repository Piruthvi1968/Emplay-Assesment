"""Validator / Critic agent: the deterministic checks, the confidence formula and the agent node (fake LLM judge)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.base import AgentContext
from app.agents.evidence import NOT_FOUND
from app.agents.validator_agent import (
    JudgeReport,
    ValidatorAgent,
    Verdict,
    confidence_for,
    datetime_problems,
    format_problem,
    key_facts,
    latest_change,
    ungrounded_facts,
)
from app.config import FieldSpec, Settings
from app.logging_utils import TraceLogger
from app.models import AddendumChange, Chunk, ChunkMeta, FieldResult, IngestReport, Source
from app.search.engine import SearchEngine
from app.search.vector_store import ChromaChunkStore, VectorStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeReranker


def _spec(type_: str) -> FieldSpec:
    return FieldSpec(name="F", group="g", type=type_, question="?")


# --- format_problem -------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2024-07-09 14:00 CST", "2024-06-10", "2024-07-09 14:00"])
def test_datetime_format_accepted(value: str) -> None:
    assert format_problem(value, _spec("datetime")) is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("July 9, 2024", "not in the format"),
        ("2024-07-09 2:00 PM CST", "not in the format"),
        ("2024-13-40 10:00 CST", "not a valid date"),
    ],
)
def test_datetime_format_rejected(value: str, message: str) -> None:
    problem = format_problem(value, _spec("datetime"))
    assert problem is not None and message in problem


def test_contact_needs_email_or_phone() -> None:
    assert "e-mail" in format_problem("Jane Doe, Procurement", _spec("contact"))
    assert format_problem("Jane Doe, 410-260-7533", _spec("contact")) is None
    assert format_problem("Jane Doe, jdoe@example.org", _spec("contact")) is None


def test_list_and_text_shapes() -> None:
    assert format_problem(["Latitude 5550"], _spec("list")) is None
    assert format_problem("Latitude 5550", _spec("list")) == "expected a list of items"
    assert format_problem(["a", "b"], _spec("text")) == "expected a single text value"
    assert format_problem("Classroom Laptops", _spec("text")) is None


# --- key_facts / ungrounded_facts -----------------------------------------------------------------


def test_key_facts_extracts_checkable_tokens() -> None:
    facts = key_facts("Due 2024-07-09 14:00 CST; jo@x.org, 410-260-7533; SKU 210-BLYZ; qty 30 and 5 items")
    assert facts == ["jo@x.org", "410-260-7533", "2024-07-09", "210-BLYZ", "30"]
    assert "14:00" not in facts and "14" not in facts and "00" not in facts  # times are not facts
    assert "5" not in facts  # single digits are too ambiguous
    assert sorted(key_facts(["Latitude 5550", "WD22TB4"])) == ["5550", "WD22TB4"]
    assert key_facts(None) == []


def test_iso_date_matches_written_date() -> None:
    cited = ["The new due date will be July 9, 2024 at 2:00 PM CST."]
    assert ungrounded_facts("2024-07-09 14:00 CST", cited) == []
    assert ungrounded_facts("2024-07-10 14:00 CST", cited) == ["2024-07-10"]


def test_phone_matches_with_other_punctuation() -> None:
    assert ungrounded_facts("Call (410) 260-7533", ["Phone: 410.260.7533"]) == []
    assert ungrounded_facts("Call 410-260-7533", ["Phone: 410.260.9999"]) == ["410-260-7533"]


def test_plain_numbers_match_on_digit_boundaries() -> None:
    assert ungrounded_facts("Due June 27; delivery in 45 days", ["Due 27-JUN-2024, 45-day delivery"]) == []
    assert ungrounded_facts("delivery in 45 days", ["delivery in 450 days"]) == ["45"]


def test_missing_id_is_reported() -> None:
    assert ungrounded_facts(["210-BLYZ", "WD22TB4"], ["Base 210-BLYZ laptop"]) == ["WD22TB4"]
    # whole-token match: '210-BLY' is not grounded by '210-BLYZ'
    assert ungrounded_facts("210-BLY", ["Base 210-BLYZ laptop"]) == ["210-BLY"]


def test_latest_change_picks_the_last_for_the_field() -> None:
    src = Source(file="a.pdf", chunk_id="a", quote="q")
    changes = [
        AddendumChange(field="Due Date", change_type="override", new_value="1", addendum="Addendum 1", source=src),
        AddendumChange(field="Title", change_type="override", new_value="t", addendum="Addendum 1", source=src),
        AddendumChange(field="Due Date", change_type="override", new_value="2", addendum="Addendum 2", source=src),
    ]
    assert latest_change(changes, "Due Date").addendum == "Addendum 2"
    assert latest_change(changes, "Payment Terms") is None


# --- confidence_for -------------------------------------------------------------------------------


def _result(conf: float, files: list[str]) -> FieldResult:
    sources = [Source(file=f, chunk_id=f"{f}#{i}", quote="q") for i, f in enumerate(files)]
    return FieldResult(value="v", sources=sources, confidence=conf)


def test_confidence_not_found() -> None:
    assert confidence_for(FieldResult(), "not_found", [], attempts=1) == 0.6
    assert confidence_for(FieldResult(), "not_found", [], attempts=2) == 0.8


def test_confidence_sources_warnings_and_failure() -> None:
    assert confidence_for(_result(0.9, ["a.pdf"]), "passed", [], 1) == 0.81  # single source x0.9
    assert confidence_for(_result(0.9, ["a.pdf", "a.pdf"]), "passed", [], 1) == 0.81  # same file twice
    assert confidence_for(_result(0.9, ["a.pdf", "b.html"]), "passed", [], 1) == 0.95  # two files agree
    assert confidence_for(_result(0.97, ["a.pdf", "b.html"]), "passed", [], 1) == 0.99  # capped
    assert confidence_for(_result(0.9, ["a.pdf"]), "passed", ["w"], 1) == 0.49  # x0.6 for warnings
    assert confidence_for(_result(0.95, ["a.pdf", "b.html"]), "failed", [], 1) == 0.3  # failed <= 0.3
    assert confidence_for(_result(0.0, ["a.pdf"]), "passed", [], 1) == 0.63  # no estimate: 0.7 start


# --- ValidatorAgent -------------------------------------------------------------------------------

RFP_TEXT = (
    "Solicitation Due: 27-JUN-2024 14:00 CST. Proposals must be submitted online through the district portal. "
    "Buyer: Jane Doe, jdoe@example.org, 214-555-0100."
)
ADD_TEXT = "The new due date will be July 9, 2024 at 2:00 PM CST."
SPECS_TEXT = "Base 210-BLYZ Dell Latitude 5550. White glove deployment with asset tagging is required."


def _chunk(cid: str, doc_type: str, text: str, addendum: int | None = None) -> Chunk:
    meta = ChunkMeta(
        chunk_id=cid, bid_id="BidV", file_name=f"{cid}.pdf", doc_type=doc_type, addendum_number=addendum, page_number=1
    )
    return Chunk(meta=meta, text=f"[BidV | {doc_type} | {cid} | p.1]\n{text}")


def _src(cid: str, quote: str) -> Source:
    return Source(file=f"{cid}.pdf", page=1, chunk_id=cid, quote=quote)


def _verdicts(mapping: dict[str, str]):
    def reply(prompt: str) -> JudgeReport:
        return JudgeReport(
            verdicts=[
                Verdict(field=f, verdict=v, reason=f"{v} reason") for f, v in mapping.items() if f"### {f}" in prompt
            ]
        )

    return reply


@pytest.fixture
def make_ctx(tmp_path: Path, settings: Settings):
    store = VectorStore(tmp_path / "chroma")
    ChromaChunkStore(store, FakeEmbedder()).upsert_chunks(
        [_chunk("rfp", "rfp", RFP_TEXT), _chunk("add1", "addendum", ADD_TEXT, 1), _chunk("specs", "specs", SPECS_TEXT)]
    )
    engine = SearchEngine(settings, store, FakeEmbedder(), FakeReranker())

    def build(judge: dict[str, str] | None = None) -> tuple[AgentContext, FakeLLM]:
        llm = FakeLLM({"JudgeReport": _verdicts(judge or {})})
        ctx = AgentContext(settings, engine, llm, TraceLogger(), lambda p: IngestReport(bid_id=p.name))
        return ctx, llm

    return build


def _state(drafts: dict[str, FieldResult], attempts: int = 1, **extra: object) -> dict:
    return {
        "fields_todo": list(drafts),
        "drafts": drafts,
        "attempts": {f: attempts for f in drafts},
        "validation": {},
        "history": {},
        "addendum_changes": [],
        "extraction_issues": {},
        **extra,
    }


def _run(ctx: AgentContext, state: dict) -> dict:
    update = ValidatorAgent(ctx)(state)
    assert "errors" not in update, update.get("errors")
    return update


def test_valid_values_pass(make_ctx) -> None:
    ctx, llm = make_ctx()
    drafts = {
        "Due Date": FieldResult(value="2024-07-09 14:00 CST", sources=[_src("add1", ADD_TEXT)], confidence=0.9),
        "contact_info": FieldResult(
            value="Jane Doe, 214-555-0100, jdoe@example.org",
            sources=[_src("rfp", "Buyer: Jane Doe, jdoe@example.org, 214-555-0100.")],
            confidence=0.9,
        ),
    }
    out = _run(ctx, _state(drafts))
    assert {f: v.status for f, v in out["validation"].items()} == {"Due Date": "passed", "contact_info": "passed"}
    assert out["retry_reasons"] == {}
    assert out["drafts"]["Due Date"].confidence == 0.81  # single source
    assert llm.calls == []  # neither field is a judge field


def test_fake_quote_fails_with_retry_reason(make_ctx) -> None:
    ctx, _ = make_ctx()
    drafts = {"Title": FieldResult(value="Classroom Laptops", sources=[_src("rfp", "Title: Gaming Consoles")])}
    out = _run(ctx, _state(drafts))
    assert out["validation"]["Title"].status == "failed"
    assert "no quote matches" in out["retry_reasons"]["Title"]
    assert out["drafts"]["Title"].confidence <= 0.3


def test_cited_chunk_must_exist(make_ctx) -> None:
    ctx, _ = make_ctx()
    drafts = {"Title": FieldResult(value="Classroom Laptops", sources=[_src("ghost", "Classroom Laptops")])}
    out = _run(ctx, _state(drafts))
    assert out["validation"]["Title"].status == "failed"
    assert "does not exist" in out["retry_reasons"]["Title"]


def test_value_without_citation_fails(make_ctx) -> None:
    ctx, _ = make_ctx()
    out = _run(ctx, _state({"Title": FieldResult(value="Classroom Laptops", confidence=0.9)}))
    assert "no citation" in out["retry_reasons"]["Title"]


def test_wrong_datetime_format_fails(make_ctx) -> None:
    ctx, _ = make_ctx()
    drafts = {
        "Due Date": FieldResult(
            value="June 27, 2024 2 PM", sources=[_src("rfp", "Solicitation Due: 27-JUN-2024 14:00 CST")]
        )
    }
    out = _run(ctx, _state(drafts))
    assert out["validation"]["Due Date"].status == "failed"
    assert "YYYY-MM-DD HH:MM TZ" in out["retry_reasons"]["Due Date"]


def test_ungrounded_facts_fail_or_warn(make_ctx) -> None:
    ctx, _ = make_ctx()
    quote = "Base 210-BLYZ Dell Latitude 5550."
    drafts = {
        "Part_no": FieldResult(value=["210-BLYZ", "WD22TB4"], sources=[_src("specs", quote)], confidence=0.9),
        "Model_no": FieldResult(value=["XPS 9999"], sources=[_src("specs", quote)], confidence=0.9),
    }
    out = _run(ctx, _state(drafts))
    part, model = out["validation"]["Part_no"], out["validation"]["Model_no"]
    assert part.status == "passed" and any("WD22TB4" in w for w in part.warnings)  # some facts grounded: warning
    assert model.status == "failed" and any("9999" in f for f in model.failures)  # none grounded: failure


def test_null_first_attempt_is_not_found_with_retry(make_ctx) -> None:
    ctx, _ = make_ctx()
    out = _run(ctx, _state({"Bid Bond Requirement": FieldResult(notes="")}, attempts=1))
    check = out["validation"]["Bid Bond Requirement"]
    assert check.status == "not_found" and check.confidence == 0.6
    assert "Bid Bond Requirement" in out["retry_reasons"]  # one wider search before accepting
    assert out["drafts"]["Bid Bond Requirement"].notes.startswith(NOT_FOUND)


def test_null_second_attempt_is_accepted(make_ctx) -> None:
    ctx, _ = make_ctx()
    out = _run(ctx, _state({"Bid Bond Requirement": FieldResult(notes="Only generic boilerplate")}, attempts=2))
    check = out["validation"]["Bid Bond Requirement"]
    assert check.status == "not_found" and check.confidence == 0.8 and check.attempts == 2
    assert out["retry_reasons"] == {}
    notes = out["drafts"]["Bid Bond Requirement"].notes
    assert notes.startswith(NOT_FOUND) and "Only generic boilerplate" in notes


def test_value_must_match_latest_addendum_change(make_ctx) -> None:
    ctx, _ = make_ctx()
    add_src = _src("add1", ADD_TEXT)
    changes = [
        AddendumChange(
            field="Due Date",
            change_type="override",
            new_value="2024-06-27 14:00 CST",
            addendum="Addendum 0",
            source=add_src,
        ),
        AddendumChange(
            field="Due Date",
            change_type="override",
            new_value="2024-07-09 14:00 CST",
            addendum="Addendum 1",
            source=add_src,
        ),
    ]
    stale = {
        "Due Date": FieldResult(
            value="2024-06-27 14:00 CST",
            sources=[_src("rfp", "Solicitation Due: 27-JUN-2024 14:00 CST")],
            confidence=0.9,
        )
    }
    out = _run(ctx, _state(stale, addendum_changes=changes))
    assert out["validation"]["Due Date"].status == "failed"
    assert "latest addendum change (Addendum 1)" in out["retry_reasons"]["Due Date"]

    current = {"Due Date": FieldResult(value="2024-07-09 14:00 CST", sources=[add_src], confidence=0.9)}
    assert _run(ctx, _state(current, addendum_changes=changes))["validation"]["Due Date"].status == "passed"


def test_extraction_issues_fail_the_field(make_ctx) -> None:
    ctx, _ = make_ctx()
    drafts = {"Title": FieldResult(notes="extraction failed: timeout")}
    out = _run(ctx, _state(drafts, extraction_issues={"Title": ["extraction failed: timeout"]}))
    assert out["validation"]["Title"].status == "failed"  # not "not_found": the extractor never answered
    assert out["retry_reasons"]["Title"] == "extraction failed: timeout"


def test_judge_no_fails_and_partial_warns(make_ctx) -> None:
    ctx, llm = make_ctx({"Bid Submission Type": "no", "Installation": "partial", "Term of Bid": "yes"})
    portal = "Proposals must be submitted online through the district portal."
    drafts = {
        "Bid Submission Type": FieldResult(value="Sealed paper copy", sources=[_src("rfp", portal)], confidence=0.9),
        "Installation": FieldResult(
            value="White glove deployment, imaging and asset tagging",
            sources=[_src("specs", "White glove deployment with asset tagging is required.")],
            confidence=0.9,
        ),
        "Term of Bid": FieldResult(value="Online portal", sources=[_src("rfp", portal)], confidence=0.9),
        "Payment Terms": FieldResult(value="Net 30", sources=[_src("rfp", "Net 30 payment")], confidence=0.9),
    }
    out = _run(ctx, _state(drafts))
    v = out["validation"]
    assert v["Bid Submission Type"].status == "failed" and "judge (no)" in out["retry_reasons"]["Bid Submission Type"]
    assert v["Installation"].status == "passed" and any("judge (partial)" in w for w in v["Installation"].warnings)
    assert v["Term of Bid"].status == "passed" and v["Term of Bid"].warnings == []
    assert v["Installation"].confidence < v["Term of Bid"].confidence
    # one batched judge call; a field that already failed a deterministic check is not sent to the judge
    judge_calls = [c for c in llm.calls if c["schema"] == "JudgeReport"]
    assert len(judge_calls) == 1 and judge_calls[0]["role"] == "fast"
    assert "### Payment Terms" not in judge_calls[0]["prompt"] and v["Payment Terms"].status == "failed"


def test_judge_unavailable_does_not_fail_fields(make_ctx) -> None:
    ctx, _ = make_ctx()
    ctx.llm = FakeLLM()  # no JudgeReport reply -> LLMError
    portal = "Proposals must be submitted online through the district portal."
    drafts = {"Bid Submission Type": FieldResult(value="Online portal", sources=[_src("rfp", portal)], confidence=0.9)}
    out = _run(ctx, _state(drafts))
    assert out["validation"]["Bid Submission Type"].status == "passed"


def test_history_grows_one_entry_per_call(make_ctx) -> None:
    ctx, _ = make_ctx()
    agent = ValidatorAgent(ctx)
    state = _state({"Title": FieldResult(value="Classroom Laptops", sources=[_src("rfp", "nope")])})
    first = agent(state)
    assert len(first["history"]["Title"]) == 1
    state = {**state, "history": first["history"], "validation": first["validation"], "attempts": {"Title": 2}}
    second = agent(state)
    assert len(second["history"]["Title"]) == 2
    assert [check.attempts for _, check in second["history"]["Title"]] == [1, 2]


def test_time_zones_written_as_words_are_accepted() -> None:
    spec = FieldSpec(name="Due Date", group="g", type="datetime", question="q")
    assert format_problem("2025-03-10 10:30 Pacific", spec) is None
    assert format_problem("2025-03-10 10:30 Eastern Time", spec) is None
    assert format_problem("2025-03-10 10:30 Pacific 1", spec) is not None
    assert datetime_problems("2025-03-10 10:30 Pacific", ["Bids due March 10, 2025, 10:30 a.m. Pacific"]) == ([], [])

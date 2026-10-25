"""The whole LangGraph run end to end: real ingest of tiny PDFs, fake embedder/re-ranker, FakeLLM (no network).

The fake LLM replies are callables that read the prompts the agents build (numbered evidence blocks and
"- <Field> (<type>...)" lines), so the tests exercise the real prompt -> citation -> validation path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from app.agents.extraction_agent import FieldDraft, GroupDraft
from app.agents.reconciliation_agent import AddendumReview, ProposedChange, ReconciliationAgent
from app.agents.report_agent import CitedQuote, DraftAnswer, DraftPart, SummaryDraft
from app.agents.validator_agent import JudgeReport, Verdict
from app.config import Settings
from app.llm_client import LLMError
from app.logging_utils import TraceLogger
from app.models import BidRecord, FieldResult, QAResponse, QueryPlan, Source, SubQuery
from app.runtime import Runtime
from app.search.engine import SearchEngine
from app.search.vector_store import VectorStore
from tests.conftest import make_pdf
from tests.fakes import FakeEmbedder, FakeLLM, FakeReranker

BID = "BidT"
RFP_PAGES = [
    "Request For Proposal 99\n"
    "Title: Classroom Laptops\n"
    "Solicitation Due: 27-JUN-2024 14:00 CST\n"
    "Proposals must be submitted online through the district portal.\n"
    "Buyer: Jane Doe, jdoe@example.org, 214-555-0100.",
    "Delivery within 45 days of award.\nThe contract term is three years with two one-year renewals.",
]
ADDENDUM_PAGES = ["ADDENDUM No. 1\nThe new due date will be July 9, 2024 at 2:00 PM CST."]
NEW_DUE = "2024-07-09 14:00 CST"
OLD_DUE = "2024-06-27 14:00 CST"

# --- reading the agents' prompts ------------------------------------------------------------------

_BLOCK = re.compile(r"^\[(\d+)\] [^\n]*\n(.*?)(?=^\[\d+\] |^## |^\(see \[|\Z)", re.M | re.S)
_FIELD_LINE = re.compile(r"^- (.+?) \((?:text|datetime|list|contact|summary)\b", re.M)


def evidence_blocks(prompt: str) -> dict[int, str]:
    """{evidence number: body} from the '[n] header\\nbody' blocks of `number_evidence`."""
    return {int(m.group(1)): m.group(2).strip() for m in _BLOCK.finditer(prompt)}


def field_names(prompt: str) -> list[str]:
    return _FIELD_LINE.findall(prompt)


def cite(prompt: str, needle: str) -> CitedQuote | None:
    """Cite the first evidence block containing `needle`, quoting the line it is on (verbatim)."""
    for n, body in evidence_blocks(prompt).items():
        for line in body.splitlines():
            if needle.lower() in line.lower():
                return CitedQuote(evidence=n, quote=line.strip())
    return None


# (value, text to cite, optional quote override); every other field is answered with null
ANSWERS: dict[str, tuple] = {
    "Bid Number": ("99", "Request For Proposal 99"),
    "Title": ("Classroom Laptops", "Title: Classroom Laptops"),
    "Due Date": (OLD_DUE, "Solicitation Due"),
    "Bid Submission Type": ("Online through the district portal", "submitted online"),
    "contact_info": ("Jane Doe, 214-555-0100, jdoe@example.org", "Buyer:"),
}


def group_reply(answers: dict[str, tuple]) -> Callable[[str], GroupDraft]:
    def reply(prompt: str) -> GroupDraft:
        drafts = []
        for name in field_names(prompt):
            value, citation = None, None
            if name in answers:
                value, needle, *quote = answers[name]
                citation = cite(prompt, needle)
                if citation and quote:
                    citation = CitedQuote(evidence=citation.evidence, quote=quote[0])
            drafts.append(
                FieldDraft(
                    field=name,
                    value=value if citation else None,
                    citations=[citation] if citation else [],
                    confidence=0.9 if citation else 0.0,
                    notes="",
                )
            )
        return GroupDraft(fields=drafts)

    return reply


def reconcile_reply(prompt: str) -> AddendumReview:
    citation = cite(prompt, "new due date")
    if not re.search(r"^- Due Date \(", prompt, re.M) or citation is None:
        return AddendumReview(changes=[])
    change = ProposedChange(
        field="Due Date",
        change_type="override",
        new_value=NEW_DUE,
        citations=[citation],
        reason="The addendum sets a new due date.",
    )
    return AddendumReview(changes=[change])


def judge_reply(prompt: str) -> JudgeReport:
    names = re.findall(r"^### (.+)$", prompt, re.M)
    return JudgeReport(verdicts=[Verdict(field=n, verdict="yes", reason="supported") for n in names])


def answer_reply(follow_ups: list[str]) -> Callable[[str], DraftAnswer]:
    def reply(prompt: str) -> DraftAnswer:
        c = cite(prompt, "new due date")
        assert c is not None, "the addendum must reach the answer agent"
        part = DraftPart(sub_question="When is the proposal due?", answer="July 9, 2024", citations=[c])
        return DraftAnswer(
            answer=f"July 9, 2024 at 2:00 PM CST [{c.evidence}].", parts=[part], follow_up_queries=follow_ups
        )

    return reply


QA_PLAN = QueryPlan(
    intent="bid_question",
    sub_queries=[SubQuery(question="When is the proposal due?", bid_ids=[BID], queries=["proposal due date"])],
)


def fake_llm(**overrides: object) -> FakeLLM:
    replies: dict[str, object] = {
        "GroupDraft": group_reply(ANSWERS),
        "AddendumReview": reconcile_reply,
        "JudgeReport": judge_reply,
        "SummaryDraft": SummaryDraft(
            summary="BidT buys classroom laptops [1], due July 9, 2024 [3].", citations=[1, 3]
        ),
        "QueryPlan": QA_PLAN,
        "DraftAnswer": answer_reply([]),
    }
    return FakeLLM({**replies, **overrides})


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture
def folder(tmp_path: Path, settings: Settings) -> Path:
    settings.paths.data_dir = tmp_path / "data"
    settings.paths.outputs_dir = tmp_path / "outputs"
    settings.paths.traces_dir = tmp_path / "outputs" / "traces"
    bid = settings.paths.data_dir / BID
    bid.mkdir(parents=True)
    make_pdf(bid / "Request for Proposal 99 Classroom Laptops.pdf", RFP_PAGES)
    make_pdf(bid / "Addendum 1 RFP 99 Classroom Laptops.pdf", ADDENDUM_PAGES)
    return bid


def make_runtime(tmp_path: Path, settings: Settings, llm: FakeLLM) -> Runtime:
    engine = SearchEngine(settings, VectorStore(tmp_path / "chroma"), FakeEmbedder(), FakeReranker())
    return Runtime(settings, engine, llm_factory=lambda trace: llm)


def trace_events(settings: Settings) -> list[dict]:
    path = settings.paths.traces_dir / f"extract_{BID}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def node_runs(events: list[dict], agent: str) -> list[dict]:
    return [e for e in events if e["step"] == "node" and e["agent"] == agent]


# --- extraction mode ------------------------------------------------------------------------------


def test_extract_end_to_end(tmp_path: Path, settings: Settings, folder: Path) -> None:
    llm = fake_llm()
    record = make_runtime(tmp_path, settings, llm).extract(folder)

    assert isinstance(record, BidRecord) and record.bid_id == BID
    assert list(record.fields) == [f.name for f in settings.fields] and len(record.fields) == 20
    assert sum(record.validation.values()) == 20
    assert list(record.field_validation) == list(record.fields)
    assert record.run_info["errors"] == []

    saved = json.loads((settings.paths.outputs_dir / f"{BID}.json").read_text(encoding="utf-8"))
    assert saved["bid_id"] == BID and list(saved["fields"]) == list(record.fields)

    status = {name: check.status for name, check in record.field_validation.items()}
    for name in ("Bid Number", "Title", "Due Date", "Bid Submission Type", "contact_info", "Bid Summary"):
        assert status[name] == "passed", (name, record.field_validation[name])
    assert record.validation == {"passed": 6, "failed": 0, "not_found": 14}

    # every cited source points at a real chunk and quotes it
    for name, result in record.fields.items():
        if result.value is not None:
            assert result.sources, name
        for src in result.sources:
            assert src.chunk_id.startswith(f"{BID}::") and src.page == 1

    events = trace_events(settings)
    for agent in (
        "orchestrator",
        "ingestion",
        "retrieval",
        "reconciliation",
        "validator",
        "orchestrator_review",
        "report",
    ):
        assert node_runs(events, agent), f"no node line for {agent}"
    assert all(e["status"] == "ok" for e in events if e["step"] == "node")
    extraction = node_runs(events, "extraction")
    assert {e["input"]["group"] for e in extraction[:3]} == {"overview_dates", "commercial_legal", "product_specs"}
    assert len(extraction) == 6  # 3 groups in round 1, 3 again for the null fields' wider retry
    assert {c["schema"] for c in llm.calls} == {"GroupDraft", "AddendumReview", "JudgeReport", "SummaryDraft"}


def test_addendum_overrides_due_date(tmp_path: Path, settings: Settings, folder: Path) -> None:
    record = make_runtime(tmp_path, settings, fake_llm()).extract(folder)
    due = record.fields["Due Date"]
    assert due.value == NEW_DUE
    assert [s.file for s in due.sources] == ["Addendum 1 RFP 99 Classroom Laptops.pdf"]
    assert "new due date" in due.sources[0].quote.lower()
    assert "Addendum 1" in due.notes and OLD_DUE in due.notes

    assert len(record.addendum_changes) == 1
    change = record.addendum_changes[0]
    assert (change.field, change.change_type, change.addendum) == ("Due Date", "override", "Addendum 1")
    assert (change.old_value, change.new_value) == (OLD_DUE, NEW_DUE)
    assert record.field_validation["Due Date"].attempts == 1  # passed first time, then frozen


def test_override_replaces_the_notes_of_the_old_value() -> None:
    source = Source(file="Addendum 2.pdf", page=1, chunk_id="c1", quote="The new due date is July 9.")
    draft = FieldResult(value=OLD_DUE, notes="The portal shows a different date")
    cited = [CitedQuote(evidence=1, quote=source.quote)]
    change = ProposedChange(
        field="Due Date", change_type="override", new_value=NEW_DUE, citations=cited, reason="Extended"
    )
    ReconciliationAgent.apply(draft, change, [source], "Addendum 2", "datetime")
    assert draft.notes == f'Changed by Addendum 2 (original: "{OLD_DUE}"). Extended.'  # stale notes dropped


def test_clarification_keeps_one_source_per_fact() -> None:
    first = Source(file="Addendum 1.pdf", page=1, chunk_id="a", quote="Quotes start in September 2024.")
    second = Source(file="Addendum 1.pdf", page=2, chunk_id="b", quote="Meet the agreed delivery deadline.")
    draft = FieldResult(value=None, notes="Not found in documents")
    cited = [CitedQuote(evidence=1, quote=first.quote), CitedQuote(evidence=2, quote=second.quote)]
    change = ProposedChange(
        field="Delivery Date",
        change_type="clarification",
        new_value="From September 2024, by the agreed deadline",
        citations=cited,
        reason="Timing answers",
    )
    record = ReconciliationAgent.apply(draft, change, [first, second], "Addendum 1", "text")
    assert draft.sources == [first, second] and record.source == first


def test_first_null_is_retried_once_then_not_found(tmp_path: Path, settings: Settings, folder: Path) -> None:
    record = make_runtime(tmp_path, settings, fake_llm()).extract(folder)
    check = record.field_validation["Payment Terms"]
    assert check.status == "not_found" and check.attempts == 2
    result = record.fields["Payment Terms"]
    assert result.value is None and result.sources == [] and result.notes.startswith("Not found in documents")
    assert result.confidence == 0.8
    assert "Payment Terms" in record.run_info["retried_fields"]


def test_retry_loop_stops_at_cap(tmp_path: Path, settings: Settings, folder: Path) -> None:
    bad = {**ANSWERS, "Title": ("Classroom Laptops", "Title:", "Title: Quantum Computers for Every Desk")}
    llm = fake_llm(GroupDraft=group_reply(bad))
    record = make_runtime(tmp_path, settings, llm).extract(folder)

    cap = 1 + settings.agents.max_validation_retries
    check = record.field_validation["Title"]
    assert check.status == "failed" and check.attempts == cap
    assert any("quote" in f for f in check.failures)
    assert record.fields["Title"].confidence <= 0.3
    assert record.validation["failed"] == 1 and sum(record.validation.values()) == 20
    # the field was re-extracted exactly `cap` times, other passed fields only once
    title_rounds = [c for c in llm.calls if c["schema"] == "GroupDraft" and "- Title (" in c["prompt"]]
    assert len(title_rounds) == cap
    assert "Previous answer rejected because" in title_rounds[-1]["prompt"]
    assert record.field_validation["Bid Number"].attempts == 1
    assert record.run_info["extraction_rounds"] == cap


def test_llm_errors_mark_fields_failed_without_crashing(tmp_path: Path, settings: Settings, folder: Path) -> None:
    llm = fake_llm(GroupDraft=LLMError("model timed out"))
    record = make_runtime(tmp_path, settings, llm).extract(folder)

    assert (settings.paths.outputs_dir / f"{BID}.json").exists()
    assert sum(record.validation.values()) == 20
    for spec in settings.fields:
        if spec.type == "summary":
            continue
        check = record.field_validation[spec.name]
        assert check.status == "failed", spec.name
        assert check.attempts == 1 + settings.agents.max_validation_retries
        assert any("extraction failed: model timed out" in f for f in check.failures), spec.name
        assert record.fields[spec.name].confidence <= 0.3


def test_node_error_is_contained(
    tmp_path: Path, settings: Settings, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: ReconciliationAgent, bid_id: str) -> list:
        raise RuntimeError("addendum store unavailable")

    monkeypatch.setattr(ReconciliationAgent, "addenda", boom)
    record = make_runtime(tmp_path, settings, fake_llm()).extract(folder)

    assert any(e.startswith("reconciliation: RuntimeError") for e in record.run_info["errors"])
    assert sum(record.validation.values()) == 20 and record.addendum_changes == []
    assert record.fields["Due Date"].value == OLD_DUE  # no reconciliation: the base value stands
    assert (settings.paths.outputs_dir / f"{BID}.json").exists()
    events = trace_events(settings)
    assert any(e["status"] == "error" for e in node_runs(events, "reconciliation"))
    assert node_runs(events, "report")


@pytest.mark.parametrize(
    ("agent", "target", "method"),
    [("validator", "ValidatorAgent", "check"), ("retrieval", "RetrievalAgent", "field_query")],
)
def test_crashing_loop_agent_does_not_spin_until_recursion_limit(
    tmp_path: Path,
    settings: Settings,
    folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    target: str,
    method: str,
) -> None:
    import app.agents.retrieval_agent as retrieval_mod
    import app.agents.validator_agent as validator_mod

    cls = {"ValidatorAgent": validator_mod.ValidatorAgent, "RetrievalAgent": retrieval_mod.RetrievalAgent}[target]

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"{agent} broke")

    monkeypatch.setattr(cls, method, boom)
    llm = fake_llm()
    record = make_runtime(tmp_path, settings, llm).extract(folder)
    errors = record.run_info["errors"]
    assert not any("GraphRecursionError" in e for e in errors), errors
    assert any(e.startswith(f"{agent}: RuntimeError") for e in errors)
    group_calls = sum(c["schema"] == "GroupDraft" for c in llm.calls)
    assert group_calls <= 3 * (1 + settings.agents.max_validation_retries)


def test_summary_ignores_failed_fields(tmp_path: Path, settings: Settings, folder: Path) -> None:
    bad = {**ANSWERS, "Title": ("Classroom Laptops", "Title:", "Title: Quantum Computers for Every Desk")}
    llm = fake_llm(GroupDraft=group_reply(bad))
    record = make_runtime(tmp_path, settings, llm).extract(folder)
    assert record.field_validation["Title"].status == "failed"
    summary_prompt = next(c["prompt"] for c in llm.calls if c["schema"] == "SummaryDraft")
    assert "Classroom Laptops" not in summary_prompt


# --- question-answering mode ----------------------------------------------------------------------


def test_ask_returns_cited_answer(tmp_path: Path, settings: Settings, folder: Path) -> None:
    llm = fake_llm()
    runtime = make_runtime(tmp_path, settings, llm)
    runtime.index(folder)
    response = runtime.ask("When is BidT due?")

    assert isinstance(response, QAResponse) and response.intent == "bid_question"
    assert response.answer == "July 9, 2024 at 2:00 PM CST [1]."
    assert response.citations and response.citations[0].file.startswith("Addendum 1")
    assert "new due date" in response.citations[0].quote.lower()
    assert [c["schema"] for c in llm.calls] == ["QueryPlan", "DraftAnswer"]


def test_follow_up_search_runs_once(tmp_path: Path, settings: Settings, folder: Path) -> None:
    # the answer agent always asks for more; the round cap must stop it after one extra retrieval
    llm = fake_llm(DraftAnswer=answer_reply(["original solicitation due date"]))
    runtime = make_runtime(tmp_path, settings, llm)
    runtime.index(folder)
    trace = TraceLogger()
    response = runtime.ask("When is BidT due?", trace=trace)

    assert response.citations
    assert sum(c["schema"] == "DraftAnswer" for c in llm.calls) == 1 + settings.agents.qa_follow_up_rounds == 2
    retrievals = [e for e in trace.events if e["agent"] == "qa_retrieval" and e["step"] == "tool_call"]
    assert len(retrievals) == 2 and retrievals[1]["sub_queries"] == ["original solicitation due date"]


def test_chitchat_is_answered_by_the_planner(tmp_path: Path, settings: Settings, folder: Path) -> None:
    llm = fake_llm(QueryPlan=QueryPlan(intent="chitchat", reply="Hello! Ask me about the indexed bids."))
    runtime = make_runtime(tmp_path, settings, llm)
    runtime.index(folder)
    response = runtime.ask("hi")
    assert response.intent == "chitchat" and response.answer == "Hello! Ask me about the indexed bids."
    assert response.citations == []
    assert [c["schema"] for c in llm.calls] == ["QueryPlan"]


def test_failed_search_is_reported_not_called_not_found(
    tmp_path: Path, settings: Settings, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = fake_llm()
    runtime = make_runtime(tmp_path, settings, llm)
    runtime.index(folder)

    def broken(self: SearchEngine, plan: QueryPlan) -> list:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(SearchEngine, "gather_evidence", broken)
    response = runtime.ask("When is BidT due?")
    assert "could not be searched" in response.answer and "database is locked" in response.answer
    assert not response.answer.startswith("Not found") and response.warnings
    assert [c["schema"] for c in llm.calls] == ["QueryPlan"]  # no answer call without evidence


def test_failed_follow_up_search_ends_the_loop(
    tmp_path: Path, settings: Settings, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = fake_llm(DraftAnswer=answer_reply(["original solicitation due date"]))
    runtime = make_runtime(tmp_path, settings, llm)
    runtime.index(folder)
    real = SearchEngine.gather_evidence
    calls: list[int] = []

    def fails_on_follow_up(self: SearchEngine, plan: QueryPlan) -> list:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("database is locked")
        return real(self, plan)

    monkeypatch.setattr(SearchEngine, "gather_evidence", fails_on_follow_up)
    response = runtime.ask("When is BidT due?")
    assert len(calls) == 2  # the first search, then the failed follow-up; no retry loop
    assert response.citations and any("database is locked" in w for w in response.warnings)
    assert sum(c["schema"] == "DraftAnswer" for c in llm.calls) == 2


def test_field_can_exclude_document_types(tmp_path: Path, settings: Settings, folder: Path) -> None:
    make_runtime(tmp_path, settings, fake_llm()).extract(folder)
    searches = {e["question"]: e["filters"] for e in trace_events(settings) if e.get("tool") == "search"}
    for spec in settings.fields:
        if spec.type != "summary":
            assert searches[spec.question]["exclude_doc_types"] == ["addendum", *spec.exclude_doc_types]
    assert any(spec.exclude_doc_types for spec in settings.fields)

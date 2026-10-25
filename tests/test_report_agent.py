from app.agents.report_agent import NOT_FOUND, CitedQuote, DraftAnswer, DraftPart, answer_question
from app.llm_client import LLMError
from app.models import Chunk, ChunkMeta, QueryPlan, SearchHit, SubEvidence, SubQuery
from tests.fakes import FakeLLM

PLAN = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="Due date?")])


def _hit(cid: str, body: str, page: int = 1) -> SearchHit:
    meta = ChunkMeta(
        chunk_id=cid, bid_id="Bid1", file_name=f"{cid}.pdf", doc_type="addendum", addendum_number=2, page_number=page
    )
    return SearchHit(chunk=Chunk(meta=meta, text=f"[Bid1 | Addendum 2 | {cid} | p.{page}]\n{body}"))


EVIDENCE = [
    SubEvidence(
        question="Due date?",
        hits=[
            _hit("a", "The new due date for this RFP will be July 9, 2024 at 2:00 PM CST."),
            _hit("b", "Solicitation Due 27-JUN-2024 14:00:00", page=2),
        ],
    )
]


def test_citations_are_renumbered_and_verified() -> None:
    draft = DraftAnswer(
        answer="Due July 9, 2024 [2], originally June 27 [1]. Also [7].",
        parts=[
            DraftPart(
                sub_question="Due date?",
                answer="July 9, 2024",
                citations=[
                    CitedQuote(evidence=2, quote="Solicitation Due 27-JUN-2024"),
                    CitedQuote(evidence=1, quote="a sentence that is not in the chunk"),
                    CitedQuote(evidence=7, quote="invented"),
                ],
            )
        ],
    )
    # In the prompt, chunk "a" is [1] and chunk "b" is [2].
    resp, follow_ups = answer_question(FakeLLM({"DraftAnswer": draft}), "When is it due?", PLAN, EVIDENCE, "run1")
    assert resp.answer == "Due July 9, 2024 [1], originally June 27 [2]. Also."  # [7] never existed
    assert [c.chunk_id for c in resp.citations] == ["b", "a"]
    assert resp.citations[0].quote == "Solicitation Due 27-JUN-2024"
    assert resp.citations[1].quote.startswith("The new due date")  # unsupported quote replaced by real text
    assert resp.citations[0].page == 2
    assert follow_ups == []


def test_no_hits_means_not_found_without_llm_call() -> None:
    llm = FakeLLM()
    resp, _ = answer_question(llm, "Bid bond?", PLAN, [SubEvidence(question="Bid bond?")], "run1")
    assert resp.answer == NOT_FOUND and llm.calls == []


def test_low_relevance_evidence_still_goes_to_the_llm() -> None:
    llm = FakeLLM({"DraftAnswer": DraftAnswer(answer=NOT_FOUND, parts=[])})
    weak = [SubEvidence(question="Bid bond?", hits=EVIDENCE[0].hits, no_evidence=True)]
    answer_question(llm, "Bid bond?", PLAN, weak, "run1")
    assert "low relevance" in llm.calls[0]["prompt"]


def test_chitchat_and_llm_failure() -> None:
    hello = QueryPlan(intent="chitchat", reply="Hi there")
    assert answer_question(FakeLLM(), "hi", hello, [], "r")[0].answer == "Hi there"
    failed, _ = answer_question(FakeLLM({"DraftAnswer": LLMError("timeout")}), "q", PLAN, EVIDENCE, "r")
    assert "could not be generated" in failed.answer and failed.citations == []


def test_follow_up_queries_are_returned() -> None:
    draft = DraftAnswer(answer="Changed to July 9 [1].", parts=[], follow_up_queries=["original due date", "x", "y"])
    _, follow_ups = answer_question(FakeLLM({"DraftAnswer": draft}), "What changed?", PLAN, EVIDENCE, "r")
    assert follow_ups == ["original due date", "x"]  # at most 2

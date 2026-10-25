from app.llm_client import LLMError
from app.models import BidInfo, QueryPlan, SubQuery
from app.search.query_planner import QueryPlanner
from tests.fakes import FakeLLM

CATALOG = [
    BidInfo(bid_id="Bid1", title="Student and Staff Computing Devices", issuer="Dallas Independent School District"),
    BidInfo(bid_id="Bid2", title="Dell Laptops w/Extended Warranty", issuer="State of Maryland Treasurer's Office"),
]
SYNONYMS = {"deadline": ["due date", "closing date"], "bond": ["bid bond", "bid security"]}


def _planner(reply: object) -> QueryPlanner:
    return QueryPlanner(FakeLLM({"QueryPlan": reply}), CATALOG, SYNONYMS, max_sub_queries=2)


def test_llm_plan_is_validated() -> None:
    plan = QueryPlan(
        intent="compare",
        sub_queries=[
            SubQuery(question="Warranty?", bid_ids=["Bid1", "Bid9"]),
            SubQuery(question="Warranty?", bid_ids=["Bid2"]),
            SubQuery(question="Third topic?"),
        ],
    )
    out = _planner(plan).plan("Compare warranties")
    assert [sq.bid_ids for sq in out.sub_queries] == [["Bid1"], ["Bid2"]]  # unknown id dropped, capped at 2


def test_single_sub_query_keeps_user_wording() -> None:
    plan = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="What is the due date?", bid_ids=["Bid1"])])
    out = _planner(plan).plan("when is bid1 due")
    assert "when is bid1 due" in out.sub_queries[0].queries


def test_chitchat_gets_a_reply_and_no_search() -> None:
    out = _planner(QueryPlan(intent="chitchat", sub_queries=[SubQuery(question="x")])).plan("hello")
    assert out.sub_queries == [] and out.reply


def test_llm_failure_falls_back_to_rules() -> None:
    out = _planner(LLMError("timeout")).plan("What is the deadline for the Dell laptops warranty bid, part 210-BLYZ?")
    sq = out.sub_queries[0]
    assert out.intent == "bid_question"
    assert sq.bid_ids == ["Bid2"]  # matched by title words
    assert sq.keywords == ["210-BLYZ"]
    assert "due date closing date" in sq.queries


def test_fallback_without_llm() -> None:
    planner = QueryPlanner(None, CATALOG, SYNONYMS)
    assert planner.plan("hi!").intent == "chitchat"
    assert planner.plan("Is a bid bond needed for Bid1?").sub_queries[0].bid_ids == ["Bid1"]
    assert planner.match_bids("anything about laptops") == []  # one shared word is not enough

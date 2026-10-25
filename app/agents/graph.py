"""LangGraph wiring: one graph, two modes (extraction and Q&A), over the shared BidState."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.base import AgentContext
from app.agents.extraction_agent import ExtractionAgent
from app.agents.ingestion_agent import IngestionAgent
from app.agents.orchestrator import Orchestrator, route_answer, route_review, route_start
from app.agents.reconciliation_agent import ReconciliationAgent
from app.agents.report_agent import ReportAgent
from app.agents.retrieval_agent import RetrievalAgent
from app.agents.state import BidState
from app.agents.validator_agent import ValidatorAgent


def build_graph(ctx: AgentContext) -> CompiledStateGraph:
    """
    extract: orchestrator -> ingest -> retrieve -> [Send x groups] extract_group -> reconcile -> validate
             -> orchestrator_review -(retry: failed fields)-> retrieve ... -(done)-> report -> END
    qa:      orchestrator -> qa_retrieve -> qa_answer -(follow-up search, once)-> qa_retrieve ... -> END
             (chitchat / out of scope: the orchestrator replies and the run ends)
    """
    orchestrator, retrieval, report = Orchestrator(ctx), RetrievalAgent(ctx), ReportAgent(ctx)
    g = StateGraph(BidState)
    g.add_node("orchestrator", orchestrator.plan)
    g.add_node("ingest", IngestionAgent(ctx))
    g.add_node("retrieve", retrieval.for_fields)
    g.add_node("extract_group", ExtractionAgent(ctx))
    g.add_node("reconcile", ReconciliationAgent(ctx))
    g.add_node("validate", ValidatorAgent(ctx))
    g.add_node("orchestrator_review", orchestrator.review)
    g.add_node("report", report.report)
    g.add_node("qa_retrieve", retrieval.for_question)
    g.add_node("qa_answer", report.answer)

    g.add_edge(START, "orchestrator")
    g.add_conditional_edges("orchestrator", route_start, {"extract": "ingest", "qa": "qa_retrieve", "done": END})
    g.add_edge("ingest", "retrieve")
    g.add_conditional_edges("retrieve", orchestrator.fan_out, ["extract_group", "reconcile"])
    g.add_edge("extract_group", "reconcile")
    g.add_edge("reconcile", "validate")
    g.add_edge("validate", "orchestrator_review")
    g.add_conditional_edges("orchestrator_review", route_review, {"retry": "retrieve", "done": "report"})
    g.add_edge("report", END)
    g.add_edge("qa_retrieve", "qa_answer")
    g.add_conditional_edges("qa_answer", route_answer, {"follow_up": "qa_retrieve", "done": END})
    return g.compile()

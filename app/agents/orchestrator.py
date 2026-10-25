"""Orchestrator / Planner: plans the job, routes work to the other agents, and owns the retry decision."""

from __future__ import annotations

import time

from langgraph.types import Send

from app.agents.base import AgentContext, agent_node
from app.agents.state import BidState
from app.models import QAResponse
from app.search.query_planner import QueryPlanner


class Orchestrator:
    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    @agent_node("orchestrator")
    def plan(self, state: BidState) -> dict:
        """Extraction: every field except the generated summary. Q&A: an intent plus sub-queries."""
        if state["mode"] == "extract":
            todo = [f.name for f in self.ctx.settings.fields if f.type != "summary"]
            return {
                "fields_todo": todo,
                "attempts": {},
                "rounds": 0,
                "retry_reasons": {},
                "addendum_changes": [],
                "validation": {},
                "history": {},
                "started_at": time.time(),
            }
        s = self.ctx.settings
        planner = QueryPlanner(self.ctx.llm, self.ctx.engine.catalog(), s.synonyms, s.search.max_sub_queries)
        plan = planner.plan(state["question"])
        update: dict = {"plan": plan, "qa_round": 0, "qa_evidence": [], "follow_ups": []}
        if not plan.sub_queries:  # chitchat / out of scope: reply directly
            update["final_output"] = QAResponse(
                question=state["question"], intent=plan.intent, answer=plan.reply or "", run_id=state["run_id"]
            )
        return update

    def fan_out(self, state: BidState) -> list[Send] | list[str]:
        """One Send per field group: the three extraction specialists run in parallel on their own fields."""
        groups: dict[str, list[str]] = {}
        for name in state["fields_todo"]:
            groups.setdefault(self.ctx.specs[name].group, []).append(name)
        if not groups:
            return ["reconcile"]
        return [
            Send(
                "extract_group",
                {
                    "group": group,
                    "fields": names,
                    "bid_id": state["bid_id"],
                    "evidence": {n: state["evidence"].get(n, []) for n in names},
                    "openings": state.get("openings", []),
                    "retry_reasons": {n: r for n, r in state.get("retry_reasons", {}).items() if n in names},
                },
            )
            for group, names in groups.items()
        ]

    @agent_node("orchestrator_review")
    def review(self, state: BidState) -> dict:
        """The feedback loop: re-run retrieval + extraction for rejected fields until the retry cap.

        A field that is still failing at the cap keeps its best attempt (fewest failed checks), stays
        "failed", and is never upgraded.
        """
        cap = 1 + self.ctx.settings.agents.max_validation_retries
        rounds = state.get("rounds", 0) + 1  # counted here, so the loop is bounded even if another agent crashed
        attempts, validation = state.get("attempts", {}), dict(state.get("validation", {}))
        order = [f.name for f in self.ctx.settings.fields]
        retry = []
        if rounds < cap:
            retry = [f for f in order if f in state.get("retry_reasons", {}) and attempts.get(f, 0) < cap]
        update: dict = {"fields_todo": retry, "rounds": rounds}

        exhausted = [
            f
            for f in state["fields_todo"]
            if f not in retry
            and f in validation
            and validation[f].status == "failed"
            and state.get("history", {}).get(f)
        ]
        if exhausted:
            drafts = {}
            for f in exhausted:
                # reversed: on a tie the latest attempt wins
                best_result, best_check = min(reversed(state["history"][f]), key=lambda rv: len(rv[1].failures))
                drafts[f] = best_result.model_copy(update={"confidence": min(best_result.confidence, 0.3)})
                validation[f] = best_check.model_copy(update={"status": "failed", "attempts": attempts.get(f, 1)})
            update |= {"drafts": drafts, "validation": validation}
        self.ctx.trace.log("orchestrator_review", "decision", round=rounds, retry=retry, gave_up=exhausted)
        return update


def route_start(state: BidState) -> str:
    if state["mode"] == "extract":
        return "extract"
    return "done" if state.get("final_output") else "qa"


def route_review(state: BidState) -> str:
    return "retry" if state["fields_todo"] else "done"


def route_answer(state: BidState) -> str:
    return "follow_up" if state.get("follow_ups") else "done"

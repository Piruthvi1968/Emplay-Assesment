"""Query planner: an LLM turns a user question into an intent plus focused sub-queries, with a rule fallback."""

from __future__ import annotations

import logging
import re

from app.agents.prompts import PLANNER_PROMPT, PLANNER_SYSTEM
from app.llm_client import LLM, LLMError
from app.models import BidInfo, QueryPlan, SubQuery
from app.search.engine import id_like_tokens

log = logging.getLogger(__name__)

_GREETING = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening))\b[\s!.?]*$", re.I)
_TITLE_WORD = re.compile(r"[a-z0-9]{4,}")
CHITCHAT_REPLY = "Hello! Ask me anything about the indexed bids, for example a due date, a contact or a requirement."


def format_catalog(catalog: list[BidInfo]) -> str:
    return "\n".join(f"- {b.bid_id}: {b.title or '(no title)'} | issuer: {b.issuer or 'unknown'}" for b in catalog)


class QueryPlanner:
    def __init__(
        self, llm: LLM | None, catalog: list[BidInfo], synonyms: dict[str, list[str]], max_sub_queries: int = 4
    ) -> None:
        self.llm = llm
        self.catalog = catalog
        self.synonyms = synonyms
        self.max_sub_queries = max_sub_queries
        self.bid_ids = {b.bid_id for b in catalog}

    def plan(self, question: str) -> QueryPlan:
        """LLM plan, validated against the index; the rule fallback if the LLM is missing or fails."""
        if self.llm is not None:
            try:
                prompt = PLANNER_PROMPT.format(catalog=format_catalog(self.catalog), question=question)
                plan = self.llm.structured(prompt, QueryPlan, system=PLANNER_SYSTEM, role="fast", agent="planner")
                return self._validate(plan, question)
            except LLMError as exc:
                log.warning("planner LLM failed, using rule fallback: %s", exc)
        return self.fallback(question)

    def _validate(self, plan: QueryPlan, question: str) -> QueryPlan:
        """Drop unknown bid ids, cap the sub-queries, and keep the user's own wording as a search text."""
        if plan.intent in ("chitchat", "out_of_scope"):
            return plan.model_copy(update={"sub_queries": [], "reply": plan.reply or CHITCHAT_REPLY})
        subs = [
            sq.model_copy(update={"bid_ids": [b for b in sq.bid_ids if b in self.bid_ids]})
            for sq in plan.sub_queries[: self.max_sub_queries]
            if sq.question.strip()
        ]
        if not subs:
            return self.fallback(question)
        if len(subs) == 1 and question not in subs[0].queries:
            subs[0] = subs[0].model_copy(update={"queries": [*subs[0].queries, question]})
        return plan.model_copy(update={"sub_queries": subs})

    def fallback(self, question: str) -> QueryPlan:
        """No LLM: greeting check, then one sub-query with synonym expansion and bid matching by id/title."""
        if _GREETING.match(question):
            return QueryPlan(intent="chitchat", reply=CHITCHAT_REPLY)
        sub = SubQuery(
            question=question,
            bid_ids=self.match_bids(question),
            queries=self.expand(question),
            keywords=id_like_tokens(question),
        )
        return QueryPlan(intent="bid_question", sub_queries=[sub])

    def expand(self, question: str) -> list[str]:
        """One extra query per synonym group whose key or phrases appear in the question."""
        q = question.lower()
        return [
            " ".join(phrases)
            for key, phrases in self.synonyms.items()
            if key in q or any(p.lower() in q for p in phrases)
        ]

    def match_bids(self, question: str) -> list[str]:
        """Bids named by id ('Bid2'), or sharing at least two significant title/issuer words with the question."""
        q = question.lower()
        words = set(_TITLE_WORD.findall(q))
        matched = []
        for b in self.catalog:
            title_words = set(_TITLE_WORD.findall(f"{b.title or ''} {b.issuer or ''}".lower()))
            if re.search(rf"\b{re.escape(b.bid_id.lower())}\b", q) or len(words & title_words) >= 2:
                matched.append(b.bid_id)
        return matched

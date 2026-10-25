"""Runtime: the one place where the search engine and LLM are wired together, shared by the CLI and the API."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from app.agents.report_agent import answer_question
from app.config import Settings, get_settings
from app.ingestion.pipeline import ingest_folder
from app.llm_client import LLM, LLMClient, LLMError
from app.logging_utils import TraceLogger
from app.models import IngestReport, QAResponse
from app.search.engine import SearchEngine
from app.search.query_planner import QueryPlanner

log = logging.getLogger(__name__)

LLMFactory = Callable[[TraceLogger], LLM]


class Runtime:
    def __init__(self, settings: Settings, engine: SearchEngine, llm_factory: LLMFactory | None) -> None:
        self.settings = settings
        self.engine = engine
        self.llm_factory = llm_factory
        self._index_lock = threading.Lock()  # one ingest at a time; searches keep running

    @classmethod
    def create(cls, settings: Settings | None = None) -> Runtime:
        s = settings or get_settings()
        factory: LLMFactory | None = None
        if s.anthropic_api_key:
            factory = lambda trace: LLMClient(s, trace)  # noqa: E731 - one client per run, so usage is per run
        else:
            log.warning("ANTHROPIC_API_KEY is not set: search works, ask/extract are disabled")
        return cls(s, SearchEngine.from_settings(s), factory)

    def bid_folders(self) -> list[Path]:
        data_dir = self.settings.paths.data_dir
        return sorted(p for p in data_dir.iterdir() if p.is_dir()) if data_dir.is_dir() else []

    def resolve_folder(self, folder: str | Path) -> Path:
        """A bid folder given by path or by name; it must exist and lie inside the data directory."""
        data_dir = self.settings.paths.data_dir.resolve()
        path = Path(folder)
        path = (path if path.is_absolute() or path.exists() else data_dir / path).resolve()
        if not path.is_dir() or not path.is_relative_to(data_dir):
            raise ValueError(f"not a bid folder inside {data_dir}: {folder}")
        return path

    def index(self, folder: Path, force: bool = False) -> IngestReport:
        with self._index_lock:
            report = ingest_folder(folder, self.engine.chunk_store(), self.settings, force=force)
            self.engine.refresh()
        return report

    def new_llm(self, trace: TraceLogger) -> LLM:
        if self.llm_factory is None:
            raise LLMError("no LLM configured: set ANTHROPIC_API_KEY, LLM_MODEL and LLM_MODEL_FAST in .env")
        return self.llm_factory(trace)

    def ask(self, question: str, trace: TraceLogger | None = None) -> QAResponse:
        """Plan -> gather evidence -> cited answer. Step 7 moves this flow into the LangGraph Q&A graph."""
        trace = trace or TraceLogger()
        llm = self.new_llm(trace)
        planner = QueryPlanner(llm, self.engine.catalog(), self.settings.synonyms, self.settings.search.max_sub_queries)
        plan = planner.plan(question)
        evidence = self.engine.gather_evidence(plan) if plan.sub_queries else []
        return answer_question(llm, question, plan, evidence, trace.run_id)

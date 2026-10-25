"""FastAPI server: /health, /index, /search, /ask. Interactive docs at /docs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from app.llm_client import LLMError
from app.models import DocType, IngestReport, QAResponse, SearchHit
from app.runtime import Runtime
from app.search.engine import chunk_body


class IndexRequest(BaseModel):
    folder: str | None = Field(None, description="Bid folder path or name inside data/, e.g. 'data/Bid1' or 'Bid1'")
    all: bool = Field(False, description="Index every folder under data/")
    force: bool = Field(False, description="Re-index unchanged files too")

    @model_validator(mode="after")
    def one_target(self) -> IndexRequest:
        if bool(self.folder) == self.all:
            raise ValueError("give exactly one of 'folder' or 'all'")
        return self


class SearchResult(BaseModel):
    rank: int
    chunk_id: str
    bid_id: str
    file: str
    page: int | None
    section: str | None
    doc_type: DocType
    addendum_number: int | None
    text: str
    rrf_score: float
    rerank_score: float | None

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResult:
        m = hit.chunk.meta
        return cls(
            rank=hit.rank,
            chunk_id=m.chunk_id,
            bid_id=m.bid_id,
            file=m.file_name,
            page=m.page_number,
            section=m.section,
            doc_type=m.doc_type,
            addendum_number=m.addendum_number,
            text=chunk_body(hit.chunk),
            rrf_score=hit.rrf_score,
            rerank_score=hit.rerank_score,
        )


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Build the app. Tests pass a ready Runtime; otherwise one is created at startup and shared."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime or Runtime.create()
        yield

    app = FastAPI(title="RFP Intelligence Platform", version="1.0", lifespan=lifespan)

    def rt(request: Request) -> Runtime:
        return request.app.state.runtime

    @app.get("/health")
    def health(request: Request) -> dict:
        r = rt(request)
        return {
            "status": "ok",
            "bids": r.engine.bids(),
            "chunks": len(r.engine.chunks()),
            "llm_configured": r.llm_factory is not None,
        }

    @app.post("/index")
    def index(body: IndexRequest, request: Request) -> list[IngestReport]:
        r = rt(request)
        try:
            folders = r.bid_folders() if body.all else [r.resolve_folder(body.folder or "")]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [r.index(folder, force=body.force) for folder in folders]

    @app.get("/search")
    def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, description="Search text")],
        bid_id: Annotated[list[str] | None, Query(description="Filter by bid (repeatable)")] = None,
        doc_type: DocType | None = None,
        addendum_number: int | None = None,
        top_k: Annotated[int, Query(ge=1, le=50)] = 5,
        mode: Literal["vector", "hybrid", "hybrid_rerank"] | None = None,
    ) -> list[SearchResult]:
        """Raw retrieval with citations; no LLM involved."""
        hits = rt(request).engine.search(
            q, top_k=top_k, bid_ids=bid_id, doc_type=doc_type, addendum_number=addendum_number, mode=mode
        )
        return [SearchResult.from_hit(h) for h in hits]

    @app.post("/ask")
    def ask(body: AskRequest, request: Request) -> QAResponse:
        try:
            return rt(request).ask(body.question)
        except LLMError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app


app = create_app()

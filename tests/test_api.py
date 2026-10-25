"""API endpoints through FastAPI's TestClient, with fake models and a fake LLM."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.report_agent import CitedQuote, DraftAnswer, DraftPart
from app.api.server import create_app
from app.config import Settings
from app.models import QueryPlan, SubQuery
from app.runtime import Runtime
from app.search.engine import SearchEngine
from app.search.vector_store import ChromaChunkStore, VectorStore
from tests.conftest import make_pdf
from tests.fakes import FakeEmbedder, FakeLLM, FakeReranker
from tests.test_engine import CHUNKS

PLAN = QueryPlan(intent="bid_question", sub_queries=[SubQuery(question="new due date", bid_ids=["Bid1"])])
DRAFT = DraftAnswer(
    answer="July 9, 2024 [1].",
    parts=[
        DraftPart(
            sub_question="new due date", answer="July 9, 2024", citations=[CitedQuote(evidence=1, quote="July 9, 2024")]
        )
    ],
)


def _runtime(tmp_path: Path, settings: Settings, with_llm: bool = True) -> Runtime:
    settings.paths.data_dir = tmp_path / "data"
    settings.paths.data_dir.mkdir()
    store = VectorStore(tmp_path / "chroma")
    ChromaChunkStore(store, FakeEmbedder()).upsert_chunks(CHUNKS)
    engine = SearchEngine(settings, store, FakeEmbedder(), FakeReranker())
    factory = (lambda trace: FakeLLM({"QueryPlan": PLAN, "DraftAnswer": DRAFT})) if with_llm else None
    return Runtime(settings, engine, factory)


@pytest.fixture
def client(tmp_path: Path, settings: Settings) -> TestClient:
    with TestClient(create_app(_runtime(tmp_path, settings))) as c:
        yield c


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["bids"] == ["Bid1", "Bid2"] and body["llm_configured"]


def test_search_with_filters_returns_citations(client: TestClient) -> None:
    results = client.get("/search", params={"q": "due date", "bid_id": "Bid1", "doc_type": "addendum"}).json()
    assert results and {r["doc_type"] for r in results} == {"addendum"}
    first = results[0]
    assert {"chunk_id", "file", "page", "section", "text", "rerank_score"} <= first.keys()
    assert not first["text"].startswith("[")  # context line removed from the returned text
    assert client.get("/search", params={"q": "x", "doc_type": "invoice"}).status_code == 422
    assert client.get("/search", params={"q": "x", "top_k": 0}).status_code == 422


def test_ask_returns_cited_answer(client: TestClient) -> None:
    body = client.post("/ask", json={"question": "When is Bid1 due?"}).json()
    assert body["answer"] == "July 9, 2024 [1]."
    assert body["citations"][0]["chunk_id"] == "b1_add2"


def test_ask_without_llm_is_503(tmp_path: Path, settings: Settings) -> None:
    with TestClient(create_app(_runtime(tmp_path, settings, with_llm=False))) as c:
        response = c.post("/ask", json={"question": "When is Bid1 due?"})
    assert response.status_code == 503 and "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_index_validates_folder(client: TestClient, tmp_path: Path) -> None:
    assert client.post("/index", json={}).status_code == 422  # neither folder nor all
    assert client.post("/index", json={"folder": "x", "all": True}).status_code == 422
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert client.post("/index", json={"folder": str(outside)}).status_code == 400
    assert client.post("/index", json={"folder": "../elsewhere"}).status_code == 400


def test_index_new_folder_then_search_it(client: TestClient, settings: Settings) -> None:
    folder = settings.paths.data_dir / "Bid3"
    folder.mkdir()
    make_pdf(folder / "Request for Proposal 77.pdf", ["Request For Proposal 77\nThe submission deadline is March 3."])
    reports = client.post("/index", json={"folder": "Bid3"}).json()
    assert reports[0]["bid_id"] == "Bid3" and reports[0]["chunks_added"] > 0
    assert "Bid3" in client.get("/health").json()["bids"]
    hits = client.get("/search", params={"q": "submission deadline", "bid_id": "Bid3"}).json()
    assert hits and hits[0]["bid_id"] == "Bid3"


def test_records_returns_saved_extraction(tmp_path: Path, settings: Settings) -> None:
    from app.agents.report_agent import fallback_record, save_record

    settings.paths.outputs_dir = tmp_path / "outputs"
    save_record(fallback_record("Bid1", settings, "boom", "run1"), settings)
    with TestClient(create_app(_runtime(tmp_path, settings))) as c:
        body = c.get("/records/Bid1").json()
        assert body["bid_id"] == "Bid1" and len(body["fields"]) == len(settings.fields)
        assert c.get("/records/Bid9").status_code == 404
        assert c.get("/records/..").status_code == 404


def test_compare_returns_markdown(tmp_path: Path, settings: Settings) -> None:
    from app.agents.report_agent import fallback_record, save_record

    settings.paths.outputs_dir = tmp_path / "outputs"
    save_record(fallback_record("Bid1", settings, "boom", "run1"), settings)
    with TestClient(create_app(_runtime(tmp_path, settings))) as c:
        resp = c.get("/compare", params={"bid_id": "Bid1"})
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
        assert "| Field | Bid1 |" in resp.text

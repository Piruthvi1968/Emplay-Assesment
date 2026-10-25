"""The Streamlit UI rendered with Streamlit's AppTest against a fake API (no server, no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

SCRIPT = str(Path(__file__).parent.parent / "app" / "ui" / "streamlit_app.py")
SOURCE = {"file": "Addendum 2.pdf", "page": 1, "section": None, "chunk_id": "c1", "quote": "The new due date"}
FIELD = {"value": "2024-07-09 14:00 CST", "sources": [SOURCE], "confidence": 0.81, "notes": "Changed by Addendum 2."}
RESPONSES: dict[str, Any] = {
    "/health": {"status": "ok", "bids": ["Bid1"], "chunks": 3, "llm_configured": True},
    "/search": [
        {
            "rank": 1,
            "bid_id": "Bid1",
            "file": "Addendum 2.pdf",
            "page": 1,
            "section": None,
            "doc_type": "addendum",
            "text": "The new due date",
            "rerank_score": 4.2,
        }
    ],
    "/records/Bid1": {
        "bid_id": "Bid1",
        "generated_at": "2026-10-25T10:00:00+00:00",
        "fields": {"Due Date": FIELD},
        "addendum_changes": [],
        "validation": {"passed": 1, "failed": 0, "not_found": 0},
        "field_validation": {"Due Date": {"status": "passed", "failures": [], "warnings": []}},
        "run_info": {"duration_s": 70, "llm_usage": {"calls": 11, "cost_usd": 0.24}},
    },
    "/compare": "| Field | Bid1 |\n|---|---|\n| **Due Date** | compared-due-date |",
    "/ask": {
        "question": "When is it due?",
        "intent": "bid_question",
        "answer": "July 9, 2024 [1].",
        "citations": [SOURCE],
        "warnings": [],
        "run_id": "r1",
    },
}


def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    path = url.split("8000", 1)[1]
    path = path.split("?", 1)[0]
    if path not in RESPONSES:
        return httpx.Response(404, json={})
    body = RESPONSES[path]
    return httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setattr(httpx, "request", fake_request)
    return AppTest.from_file(SCRIPT, default_timeout=60).run()


def test_pages_render_from_the_api(app: AppTest) -> None:
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Ask", "Search", "Extracted bids", "Compare"]
    assert any("compared-due-date" in m.value for m in app.markdown)  # the Compare tab's table
    assert [(m.label, m.value) for m in app.metric][0] == ("✅ passed", "1")  # Bid1's saved record
    app.text_input[0].set_value("due date")
    app.button[0].click().run()
    assert any(m.value.startswith("**1. Addendum 2.pdf**") for m in app.markdown)


def test_chat_shows_answer_with_sources(app: AppTest) -> None:
    app.chat_input[0].set_value("When is it due?").run()
    assert not app.exception
    assert any(m.value == "July 9, 2024 [1]." for m in app.markdown)
    assert any("Addendum 2.pdf" in m.value for m in app.markdown)

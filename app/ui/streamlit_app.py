"""Web UI (Streamlit): cited Q&A chat, raw search with filters, the extracted bid records and their comparison.

A thin client of the REST API (`RFP_API_URL`, default http://localhost:8000), so the UI, the CLI and the API all
run the same pipeline. `RFP_PUBLIC_API_URL` is the address shown to the browser (in Docker the UI reaches the API at
http://api:8000, which only exists inside the Docker network).
Start it with `python main.py ui` (the API must be running: `python main.py serve`).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_URL = os.getenv("RFP_API_URL", "http://localhost:8000").rstrip("/")
PUBLIC_API_URL = os.getenv("RFP_PUBLIC_API_URL", API_URL).rstrip("/")
DOC_TYPES = ["bid_page", "rfp", "addendum", "specs", "affidavit", "form", "other"]
STATUS_ICON = {"passed": "✅ passed", "failed": "❌ failed", "not_found": "➖ not found"}


def api(method: str, path: str, *, timeout: float = 30, missing_ok: bool = False, **kwargs: Any) -> Any:
    """Call the API and return its JSON (or text); errors are shown on the page instead of a traceback."""
    try:
        resp = httpx.request(method, f"{API_URL}{path}", timeout=timeout, **kwargs)
    except httpx.HTTPError as exc:
        st.error(f"The API is not reachable at {API_URL} ({exc}). Start it with `python main.py serve`.")
        st.stop()
    if resp.status_code == 404 and missing_ok:
        return None
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        st.error(f"API error {resp.status_code}: {detail}")
        st.stop()
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text


def where(source: dict) -> str:
    return f"p.{source['page']}" if source.get("page") is not None else (source.get("section") or "web page")


def show_sources(sources: list[dict]) -> None:
    for i, src in enumerate(sources, start=1):
        st.markdown(f"**[{i}]** {src['file']} · {where(src)}")
        if src.get("quote"):
            st.caption(f"“{src['quote']}”")


def show_answer(response: dict) -> None:
    with st.chat_message("user"):
        st.write(response["question"])
    with st.chat_message("assistant"):
        st.markdown(response["answer"])
        st.caption(f"intent: {response['intent']} · run {response['run_id']}")
        for warning in response.get("warnings", []):
            st.warning(f"Answer check: {warning}")
        if response["citations"]:
            with st.expander(f"Sources ({len(response['citations'])})"):
                show_sources(response["citations"])


def chat_tab() -> None:
    st.caption("Answers come only from the indexed documents, with citations. Each question is answered on its own.")
    history: list[dict] = st.session_state.setdefault("history", [])
    for response in history:
        show_answer(response)
    if question := st.chat_input("Ask about the bids, e.g. What changed in Addendum 2 compared to the original RFP?"):
        with st.spinner("Planning, searching and answering…"):
            response = api("POST", "/ask", json={"question": question}, timeout=300)
        history.append(response)
        show_answer(response)


def search_tab(bids: list[str]) -> None:
    st.caption("Raw retrieval (no LLM): hybrid dense + BM25 search, fused with RRF and re-ranked.")
    with st.form("search"):
        query = st.text_input("Search text", placeholder="e.g. 210-BLYZ, bid bond, delivery")
        cols = st.columns(4)
        chosen = cols[0].multiselect("Bids", bids)
        doc_type = cols[1].selectbox("Document type", ["any", *DOC_TYPES])
        mode = cols[2].selectbox("Mode", ["hybrid_rerank", "hybrid", "vector"])
        top_k = cols[3].slider("Results", 1, 20, 5)
        submitted = st.form_submit_button("Search")
    if not (submitted and query.strip()):
        return
    params = [("q", query), ("top_k", top_k), ("mode", mode), *(("bid_id", b) for b in chosen)]
    if doc_type != "any":
        params.append(("doc_type", doc_type))
    hits = api("GET", "/search", params=params, timeout=120)
    if not hits:
        st.info("No passages match these filters.")
    for hit in hits:
        score = f" · re-rank {hit['rerank_score']:.2f}" if hit["rerank_score"] is not None else ""
        st.markdown(f"**{hit['rank']}. {hit['file']}** · {hit['bid_id']} · {where(hit)} · `{hit['doc_type']}`{score}")
        with st.expander("Passage"):
            st.text(hit["text"])


def show_record(record: dict) -> None:
    counts = record["validation"]
    cols = st.columns(3)
    for col, status in zip(cols, ("passed", "failed", "not_found"), strict=True):
        col.metric(STATUS_ICON[status], counts.get(status, 0))
    checks = record.get("field_validation", {})
    rows = []
    for name, result in record["fields"].items():
        value = result["value"]
        rows.append(
            {
                "Field": name,
                "Value": "; ".join(value) if isinstance(value, list) else (value or "—"),
                "Status": STATUS_ICON.get(checks.get(name, {}).get("status", ""), ""),
                "Confidence": result["confidence"],
                "Source": ", ".join(f"{s['file']} {where(s)}" for s in result["sources"][:2]),
            }
        )
    st.dataframe(rows, hide_index=True, width="stretch")

    name = st.selectbox("Field details", list(record["fields"]))
    result = record["fields"][name]
    if result["notes"]:
        st.info(result["notes"])
    for problem in checks.get(name, {}).get("failures", []) + checks.get(name, {}).get("warnings", []):
        st.warning(problem)
    show_sources(result["sources"])

    if record["addendum_changes"]:
        st.subheader("Addendum changes")
        st.dataframe(
            [
                {
                    "Field": c["field"],
                    "Type": c["change_type"],
                    "Addendum": c["addendum"],
                    "Old": str(c["old_value"]),
                    "New": str(c["new_value"]),
                }
                for c in record["addendum_changes"]
            ],
            hide_index=True,
            width="stretch",
        )
    usage = record["run_info"].get("llm_usage") or {}
    st.caption(
        f"Generated {record['generated_at']} · {record['run_info'].get('duration_s', '?')} s · "
        f"{usage.get('calls', '?')} LLM calls · ${usage.get('cost_usd', 0):.2f}"
    )


def bids_tab(bids: list[str], llm_ready: bool) -> None:
    if not bids:
        st.info("No bids are indexed yet. Run `python main.py index --all`.")
        return
    bid = st.selectbox("Bid", bids)
    if st.button("Run extraction (about a minute, uses the LLM)", disabled=not llm_ready):
        with st.spinner(f"Running the extraction agents on {bid}…"):
            api("POST", "/extract", json={"bid_id": bid}, timeout=900)
    record = api("GET", f"/records/{bid}", missing_ok=True)
    if record is None:
        st.info(f"{bid} has not been extracted yet. Use the button above.")
    else:
        show_record(record)


def compare_tab() -> None:
    st.caption("Side-by-side view of the saved extraction records (no LLM).")
    st.markdown(api("GET", "/compare", timeout=60), unsafe_allow_html=True)  # cells use <br> line breaks


def main() -> None:
    st.set_page_config(page_title="RFP Intelligence", page_icon="📑", layout="wide")
    health = api("GET", "/health")
    with st.sidebar:
        st.header("RFP Intelligence")
        st.write(f"Bids: {', '.join(health['bids']) or 'none'}")
        st.write(f"Indexed chunks: {health['chunks']}")
        st.write("LLM: " + ("configured" if health["llm_configured"] else "not configured (search only)"))
        st.caption(f"API: {PUBLIC_API_URL} · Swagger: {PUBLIC_API_URL}/docs")
    ask, search, records, compare = st.tabs(["Ask", "Search", "Extracted bids", "Compare"])
    with ask:
        chat_tab()
    with search:
        search_tab(health["bids"])
    with records:
        bids_tab(health["bids"], health["llm_configured"])
    with compare:
        compare_tab()


main()

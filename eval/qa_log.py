"""Q&A log: runs a fixed question set through the Q&A graph and writes outputs/qa_log.md.

The first five questions are the brief's examples; the rest cover contacts, part numbers, submission,
delivery and payment, contract vehicles, a pre-bid meeting, a greeting and an out-of-scope question.
Every answer is listed with its intent, citations (file, page or HTML section, quote), latency and cost.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.logging_utils import TraceLogger
from app.models import QAResponse
from app.runtime import Runtime

QUESTIONS = [
    "What is the submission deadline for Bid1 after all addendums?",
    "Which affidavits are required for the Dell laptop bid?",
    "Compare the warranty requirements of both bids.",
    "Is a bid bond required for Bid1, and how much?",
    "What changed in Addendum 2 compared to the original RFP?",
    "hi",
    "What is 210-BLYZ?",
    "Who is the procurement contact for the Maryland Treasurer bid, and how do I reach them?",
    "How must proposals be submitted for the Dallas ISD bid?",
    "When must the Dell laptops be delivered, and how are invoices paid?",
    "Which cooperative or master contract applies to each bid?",
    "Is there a pre-bid meeting for Bid1? Is attendance mandatory?",
    "What is the weather in Chennai today?",
]


def llm_usage(trace: TraceLogger, prices: dict[str, tuple[float, float]]) -> dict[str, Any]:
    calls = [e["llm"] for e in trace.events if e["step"] == "llm_call"]
    cost = 0.0
    for c in calls:
        price_in, price_out = prices.get(c["model"], (0.0, 0.0))
        cost += (c["in_tokens"] * price_in + c["out_tokens"] * price_out) / 1_000_000
    return {
        "calls": len(calls),
        "in_tokens": sum(c["in_tokens"] for c in calls),
        "out_tokens": sum(c["out_tokens"] for c in calls),
        "cost_usd": cost,
    }


def format_entry(n: int, question: str, response: QAResponse, seconds: float, usage: dict[str, Any]) -> list[str]:
    lines = [f"## {n}. {question}", "", f"**Intent:** `{response.intent}`", "", response.answer.strip(), ""]
    if response.citations:
        lines.append("**Citations**")
        lines.append("")
        for k, src in enumerate(response.citations, start=1):
            where = f"p.{src.page}" if src.page is not None else (src.section or "html")
            quote = " ".join(src.quote.split())
            lines.append(f'- [{k}] {src.file}, {where} — "{quote}"')
        lines.append("")
    if response.warnings:
        lines += ["**Answer checks**", "", *(f"- {w}" for w in response.warnings), ""]
    lines.append(
        f"*Latency {seconds:.1f} s · {usage['calls']} LLM calls · "
        f"{usage['in_tokens']}/{usage['out_tokens']} tokens in/out · ${usage['cost_usd']:.4f}*"
    )
    lines.append("")
    return lines


def main(out_path: Path | None = None) -> int:
    settings = get_settings()
    out_path = out_path or settings.paths.outputs_dir / "qa_log.md"
    runtime = Runtime.create(settings)
    prices = settings.llm.prices_per_mtok

    body: list[str] = []
    total_cost = total_s = 0.0
    for n, question in enumerate(QUESTIONS, start=1):
        trace = TraceLogger()
        start = time.perf_counter()
        response = runtime.ask(question, trace)
        seconds = time.perf_counter() - start
        usage = llm_usage(trace, prices)
        total_cost += usage["cost_usd"]
        total_s += seconds
        body += format_entry(n, question, response, seconds, usage)
        print(f"{n:>2}. [{response.intent}] {seconds:5.1f}s ${usage['cost_usd']:.4f}  {question}")

    header = [
        "# Q&A log",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python -m eval.qa_log`. "
        f"{len(QUESTIONS)} questions; the first five are the brief's examples.",
        f"Models: `{settings.llm_model}` (answers), `{settings.llm_model_fast}` (query planning). "
        f"Total LLM cost ${total_cost:.4f}, total time {total_s:.0f} s.",
        "",
        "Pages are physical PDF pages (1-based); BidNet HTML citations give the section heading instead.",
        "",
    ]
    out_path.write_text("\n".join(header + body), encoding="utf-8")
    print(f"total ${total_cost:.4f}, {total_s:.0f}s -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

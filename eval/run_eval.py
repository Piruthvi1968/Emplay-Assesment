"""Retrieval evaluation: Recall@k and MRR for vector / hybrid / hybrid_rerank on eval/eval_set.json.

No LLM is involved: each question goes straight to SearchEngine.search() (with the question's bid
filter), so the numbers are cheap to reproduce. Expected passages are matched by file name + page +
text substring, not by chunk_id, so the labels survive chunking changes.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings
from app.models import Chunk
from app.search.engine import SearchEngine

EVAL_DIR = PROJECT_ROOT / "eval"
MODES = ["vector", "hybrid", "hybrid_rerank"]
KS = [1, 3, 5, 10]
K_MAX = max(KS)

_CHAR_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_CHAR_MAP)).strip().lower()


def is_relevant(chunk: Chunk, expected: list[dict[str, Any]]) -> bool:
    """A hit is relevant if it matches ANY expected passage (file substring + page + text substring)."""
    m, body = chunk.meta, normalize(chunk.text)
    return any(
        e["file_contains"].lower() in m.file_name.lower()
        and e.get("page") == m.page_number
        and normalize(e["text_contains"]) in body
        for e in expected
    )


def run_mode(engine: SearchEngine, items: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    rows = []
    for item in items:
        bids = [item["bid_id"]] if item.get("bid_id") else None
        start = time.perf_counter()
        hits = engine.search(item["question"], top_k=K_MAX, bid_ids=bids, mode=mode)
        ms = (time.perf_counter() - start) * 1000
        flags = [is_relevant(h.chunk, item["expected"]) for h in hits]
        first = next((i + 1 for i, f in enumerate(flags) if f), None)
        rows.append(
            {
                "id": item["id"],
                "type": item.get("type", ""),
                "rank": first,
                "ms": ms,
                "answerable": bool(item["expected"]),
                "top_score": hits[0].rerank_score if hits else None,
                "relevant_score": hits[first - 1].rerank_score if first else None,
                "best_irrelevant": max(
                    (h.rerank_score for h, f in zip(hits, flags, strict=True) if not f and h.rerank_score is not None),
                    default=None,
                ),
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    scored = [r for r in rows if r["answerable"]]
    n = len(scored) or 1
    out = {f"R@{k}": sum(1 for r in scored if r["rank"] and r["rank"] <= k) / n for k in KS}
    out["MRR"] = sum(1 / r["rank"] for r in scored if r["rank"]) / n
    out["ms"] = statistics.mean(r["ms"] for r in rows)
    return out


def calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-encoder score distributions used to pick search.not_found_threshold.

    The threshold is applied to a sub-query's BEST score (see SearchEngine.gather_evidence), so the
    comparison that matters is top-1 score of answerable questions vs top-1 of not-found questions.
    """
    top = [r["top_score"] for r in rows if r["answerable"] and r["top_score"] is not None]
    rel = [r["relevant_score"] for r in rows if r["answerable"] and r["relevant_score"] is not None]
    irr = [r["best_irrelevant"] for r in rows if r["answerable"] and r["best_irrelevant"] is not None]
    nf = [r["top_score"] for r in rows if not r["answerable"] and r["top_score"] is not None]
    suggestion = None
    if top and nf and max(nf) < min(top):
        suggestion = round((max(nf) + min(top)) / 2, 2)
    return {
        "answerable_top1": top,
        "relevant_first_hit": rel,
        "best_irrelevant": irr,
        "not_found_top1": nf,
        "suggested": suggestion,
    }


def _fmt_dist(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    return f"min {min(xs):+.2f} / median {statistics.median(xs):+.2f} / max {max(xs):+.2f} (n={len(xs)})"


def write_report(
    items: list[dict[str, Any]],
    results: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, float]],
    calib: dict[str, Any],
    path: Path,
) -> None:
    s = get_settings()
    n_ans = sum(1 for i in items if i["expected"])
    lines = [
        "# Retrieval evaluation report",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `python main.py eval`. "
        f"{len(items)} questions ({n_ans} answerable; not-found questions are excluded from Recall/MRR).",
        f"Embedding `{s.embedding.model}`, re-ranker `{s.search.rerank_model}`, "
        f"{s.search.candidates_per_list} candidates per list, RRF k={s.search.rrf_k}, "
        f"{s.search.rerank_candidates} re-rank candidates. Questions are sent as-is (no LLM query planner).",
        "",
        "## Summary",
        "",
        "| Configuration | " + " | ".join(f"Recall@{k}" for k in KS) + " | MRR@10 | Avg latency (ms) |",
        "|---|" + "---|" * (len(KS) + 2),
    ]
    for mode in MODES:
        sm = summaries[mode]
        lines.append(
            f"| {mode} | " + " | ".join(f"{sm[f'R@{k}']:.2f}" for k in KS) + f" | {sm['MRR']:.3f} | {sm['ms']:.0f} |"
        )
    lines += [
        "",
        "Recall@k = share of questions with at least one relevant passage in the top k. "
        "MRR = mean of 1/rank of the first relevant passage (0 if none in the top 10).",
        "",
        "## Rank of the first relevant passage per question",
        "",
        "| id | type | " + " | ".join(MODES) + " | question |",
        "|---|---|" + "---|" * len(MODES) + "---|",
    ]
    by_id = {m: {r["id"]: r for r in results[m]} for m in MODES}
    for item in items:
        ranks = []
        for m in MODES:
            r = by_id[m][item["id"]]
            ranks.append("n/a" if not r["answerable"] else (str(r["rank"]) if r["rank"] else "miss"))
        lines.append(f"| {item['id']} | {item.get('type', '')} | " + " | ".join(ranks) + f" | {item['question']} |")
    lines += [
        "",
        "## Not-found threshold calibration (hybrid_rerank, cross-encoder logits)",
        "",
        f"- Top-1 score for answerable questions: {_fmt_dist(calib['answerable_top1'])}",
        f"- Score of the first relevant hit (answerable questions): {_fmt_dist(calib['relevant_first_hit'])}",
        f"- Best irrelevant hit per answerable question: {_fmt_dist(calib['best_irrelevant'])}",
        f"- Top-1 score for not-found questions: {_fmt_dist(calib['not_found_top1'])}",
        "- Suggested `search.not_found_threshold`: "
        + (
            f"{calib['suggested']} (midpoint between the highest not-found top-1 and the lowest answerable top-1)"
            if calib["suggested"] is not None
            else "no clean separation on this set; keep the configured value"
        ),
        f"- Configured value: {s.search.not_found_threshold}",
        "",
        "Irrelevant hits can score high when they are on-topic (for example the RFP header next to the "
        "addendum that changed the date), so the threshold only flags questions with *no* plausible evidence.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(eval_path: Path = EVAL_DIR / "eval_set.json", report_path: Path = EVAL_DIR / "report.md") -> int:
    items = json.loads(eval_path.read_text(encoding="utf-8"))
    engine = SearchEngine.from_settings()
    if not engine.chunks():
        print("Index is empty. Run `python main.py index --all` first.")
        return 1
    engine.search("warm-up", top_k=1)  # load both models before timing anything

    results = {m: run_mode(engine, items, m) for m in MODES}
    summaries = {m: summarize(results[m]) for m in MODES}
    calib = calibration(results["hybrid_rerank"])
    write_report(items, results, summaries, calib, report_path)

    print(f"{'mode':<14}" + "".join(f"R@{k:<6}" for k in KS) + "MRR     ms")
    for m in MODES:
        sm = summaries[m]
        print(f"{m:<14}" + "".join(f"{sm[f'R@{k}']:<8.2f}" for k in KS) + f"{sm['MRR']:<8.3f}{sm['ms']:.0f}")
    print(f"not-found threshold suggestion: {calib['suggested']}  (report: {report_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

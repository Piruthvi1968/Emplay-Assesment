"""CLI entry point: index / extract / ask / search / eval / serve."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from app.runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="RFP Intelligence Platform")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="Ingest a bid folder (incremental)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bid", help="Path to a bid folder, e.g. ./data/Bid1")
    g.add_argument("--all", action="store_true", help="Every folder under data/")
    p.add_argument("--force", action="store_true", help="Re-index even unchanged files")

    p = sub.add_parser("extract", help="Index + run the extraction agents -> outputs/<bid>.json")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bid", help="Path to a bid folder")
    g.add_argument("--all", action="store_true", help="Every folder under data/")

    p = sub.add_parser("ask", help="Ask a free-form question (cited answer)")
    p.add_argument("question")

    p = sub.add_parser("search", help="Raw retrieval, no LLM")
    p.add_argument("query")
    p.add_argument("--bid", action="append", dest="bids", help="Filter by bid_id (repeatable)")
    p.add_argument("--doc-type", default=None)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument(
        "--mode",
        choices=["vector", "hybrid", "hybrid_rerank"],
        default=None,
        help="Default: search.default_mode in config.yaml",
    )

    p = sub.add_parser("eval", help="Retrieval evaluation -> eval/report.md")
    p.add_argument("--min-mrr", type=float, help="Exit 1 if hybrid_rerank MRR is below this (CI gate)")

    p = sub.add_parser("serve", help="Start the FastAPI server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("compare", help="Side-by-side report of the extracted bids -> outputs/comparison.md")
    p.add_argument("--bid", dest="bids", action="append", help="Bid id to include (repeatable; default: all)")

    p = sub.add_parser("ui", help="Start the web UI (Streamlit); needs the API from 'serve'")
    p.add_argument("--port", type=int, default=8501)
    return parser


def _bid_folders(runtime: Runtime, args: argparse.Namespace) -> list[Path]:
    if args.all:
        return runtime.bid_folders()
    try:
        return [runtime.resolve_folder(args.bid)]
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def cmd_index(args: argparse.Namespace) -> int:
    runtime = Runtime.create()
    for folder in _bid_folders(runtime, args):
        r = runtime.index(folder, force=args.force)
        print(
            f"{r.bid_id}: indexed={len(r.files_indexed)} skipped={len(r.files_skipped)} "
            f"failed={len(r.files_failed)} chunks_added={r.chunks_added}"
        )
        for w in r.warnings:
            print(f"  warning: {w}")
    print(f"index now holds {len(runtime.engine.chunks())} chunks")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from app.search.engine import SearchEngine

    engine = SearchEngine.from_settings()
    hits = engine.search(args.query, top_k=args.top_k, bid_ids=args.bids, doc_type=args.doc_type, mode=args.mode)
    if not hits:
        print("no hits")
    for h in hits:
        m = h.chunk.meta
        where = f"p.{m.page_number}" if m.page_number is not None else (m.section or "html")
        score = f"rerank={h.rerank_score:.2f}" if h.rerank_score is not None else f"rrf={h.rrf_score:.4f}"
        print(f"#{h.rank} [{m.bid_id} | {m.doc_type} | {m.file_name} | {where}] {score}")
        body = h.chunk.text.split("\n", 1)[-1]  # drop the context prefix line
        print("   " + body[:300].replace("\n", " ") + ("..." if len(body) > 300 else ""))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from app.llm_client import LLMError
    from app.logging_utils import TraceLogger

    trace = TraceLogger()
    try:
        response = Runtime.create().ask(args.question, trace)
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"intent: {response.intent}\n\n{response.answer}\n")
    for i, src in enumerate(response.citations, start=1):
        where = f"p.{src.page}" if src.page is not None else (src.section or "")
        print(f'[{i}] {src.file} {where}\n    "{src.quote[:200]}"')
    for warning in response.warnings:
        print(f"check: {warning}")
    calls = [e["llm"] for e in trace.events if e["step"] == "llm_call"]
    print(
        f"\nLLM calls: {len(calls)}, tokens in/out: {sum(c['in_tokens'] for c in calls)}/"
        f"{sum(c['out_tokens'] for c in calls)}"
    )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from app.llm_client import LLMError

    runtime = Runtime.create()
    for folder in _bid_folders(runtime, args):
        try:
            record = runtime.extract(folder)
        except LLMError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"\n{record.bid_id}: {record.validation}  ->  outputs/{record.bid_id}.json")
        for name, result in record.fields.items():
            check = record.field_validation[name]
            value = json.dumps(result.value, ensure_ascii=False) if result.value is not None else "-"
            print(f"  {check.status:<9} {result.confidence:4.2f}  {name:<38} {value[:80]}")
        for change in record.addendum_changes:
            print(f"  {change.addendum}: {change.change_type} {change.field} -> {json.dumps(change.new_value)[:70]}")
        print(f"  run: {record.run_info.get('llm_usage')}  {record.run_info.get('duration_s')}s")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from eval.run_eval import main as run_eval

    return run_eval(min_mrr=args.min_mrr)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("app.api.server:app", host=args.host, port=args.port)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from app.comparison import write_comparison
    from app.config import get_settings

    path = write_comparison(get_settings().paths.outputs_dir, args.bids)
    print(f"comparison written to {path}")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import subprocess

    script = Path(__file__).parent / "app" / "ui" / "streamlit_app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(args.port)]
    return subprocess.call([*cmd, "--browser.gatherUsageStats", "false"])


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "chromadb", "fastembed", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("--bid", "--all"):  # the brief's one-command form: main.py --bid ./data/Bid1
        argv = ["extract", *argv]
    args = build_parser().parse_args(argv)
    commands = {
        "index": cmd_index,
        "extract": cmd_extract,
        "ask": cmd_ask,
        "search": cmd_search,
        "eval": cmd_eval,
        "serve": cmd_serve,
        "compare": cmd_compare,
        "ui": cmd_ui,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

"""CLI entry point: index / extract / ask / search / eval / serve."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.config import get_settings


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

    sub.add_parser("eval", help="Retrieval evaluation -> eval/report.md")

    p = sub.add_parser("serve", help="Start the FastAPI server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    return parser


def _bid_folders(args: argparse.Namespace) -> list[Path]:
    if args.all:
        data_dir = get_settings().paths.data_dir
        return sorted(p for p in data_dir.iterdir() if p.is_dir())
    folder = Path(args.bid)
    if not folder.is_dir():
        raise SystemExit(f"Not a folder: {folder}")
    return [folder]


def cmd_index(args: argparse.Namespace) -> int:
    from app.ingestion.pipeline import ingest_folder
    from app.search.engine import SearchEngine

    settings = get_settings()
    engine = SearchEngine.from_settings(settings)
    for folder in _bid_folders(args):
        r = ingest_folder(folder, engine.chunk_store(), settings, force=args.force)
        print(
            f"{r.bid_id}: indexed={len(r.files_indexed)} skipped={len(r.files_skipped)} "
            f"failed={len(r.files_failed)} chunks_added={r.chunks_added}"
        )
        for w in r.warnings:
            print(f"  warning: {w}")
    print(f"index now holds {engine.store.count()} chunks")
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "chromadb", "fastembed", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    if args.command == "index":
        return cmd_index(args)
    if args.command == "search":
        return cmd_search(args)
    if args.command == "eval":
        from eval.run_eval import main as run_eval

        return run_eval()
    print(f"'{args.command}' is not implemented yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

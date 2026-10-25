"""ingest_folder(): label -> parse -> clean -> chunk -> store, with a hash manifest for incremental runs."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Protocol

from app.config import Settings, get_settings
from app.ingestion.chunker import chunk_doc
from app.ingestion.cleaning import clean_doc, normalize_chars
from app.ingestion.detect import assign_doc_types, detect_addendum_number, detect_doc_date, detect_doc_type
from app.ingestion.html_parser import parse_html
from app.ingestion.pdf_parser import first_page_text, parse_pdf
from app.models import Chunk, DocType, IngestReport, ParsedDoc

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".html", ".htm"}


class ChunkStore(Protocol):
    """Where chunks go: `ChromaChunkStore` in the app, an in-memory store in tests."""

    def upsert_chunks(self, chunks: list[Chunk]) -> None: ...
    def delete_file(self, bid_id: str, file_name: str) -> None: ...
    def count_file(self, bid_id: str, file_name: str) -> int: ...


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_manifest(path: Path, manifest: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _first_text(path: Path) -> str:
    """Text used for labelling: first page(s) of a PDF, or the parsed text of an HTML page."""
    if path.suffix.lower() == ".pdf":
        return normalize_chars(first_page_text(path))
    doc = parse_html(path)
    return "\n".join(b.text for p in doc.pages for b in p.blocks)


def parse_file(path: Path, settings: Settings) -> ParsedDoc:
    if path.suffix.lower() == ".pdf":
        doc = parse_pdf(path, settings.ingestion.min_chars_per_page)
    else:
        doc = parse_html(path)
    return clean_doc(doc, settings.ingestion.header_footer_min_repeat)


def ingest_folder(
    folder: Path, store: ChunkStore, settings: Settings | None = None, force: bool = False
) -> IngestReport:
    """Index one bid folder. Unchanged files (same sha256) are skipped; one bad file never stops the run."""
    settings = settings or get_settings()
    bid_id = folder.name
    report = IngestReport(bid_id=bid_id)
    manifest = load_manifest(settings.paths.manifest)
    known = manifest.get(bid_id, {})

    files = sorted(p for p in folder.iterdir() if p.is_file())
    for p in files:
        if p.suffix.lower() not in SUPPORTED:
            report.warnings.append(f"unsupported file skipped: {p.name}")
    files = [p for p in files if p.suffix.lower() in SUPPORTED]

    # Files removed from the folder since the last run: drop their chunks.
    for gone in set(known) - {p.name for p in files}:
        store.delete_file(bid_id, gone)
        known.pop(gone)
        report.warnings.append(f"file removed, chunks deleted: {gone}")

    # Labels need the whole folder (the "largest PDF is the RFP" fallback), so label every file.
    first_texts: dict[str, str] = {}
    for p in files:
        try:
            first_texts[p.name] = _first_text(p)
        except Exception as exc:
            first_texts[p.name] = ""
            report.warnings.append(f"could not read {p.name} for labelling: {exc}")
    detected = {name: detect_doc_type(name, text) for name, text in first_texts.items()}
    doc_types: dict[str, DocType] = assign_doc_types(detected, {p.name: p.stat().st_size for p in files})

    for p in files:
        digest = sha256_file(p)
        # Skip only if the file is unchanged AND its chunks are still in the store (the index
        # folder may have been deleted while the manifest survived).
        if not force and known.get(p.name) == digest and store.count_file(bid_id, p.name) > 0:
            report.files_skipped.append(p.name)
            continue
        try:
            doc = parse_file(p, settings)
            text = first_texts[p.name]
            doc_type = doc_types[p.name]
            chunks = chunk_doc(
                doc,
                bid_id=bid_id,
                doc_type=doc_type,
                addendum_number=detect_addendum_number(p.name, text) if doc_type == "addendum" else None,
                doc_date=detect_doc_date(text),
                cfg=settings.ingestion,
            )
            store.delete_file(bid_id, p.name)  # changed file: old chunks go first
            store.upsert_chunks(chunks)
            known[p.name] = digest
            report.files_indexed.append(p.name)
            report.chunks_added += len(chunks)
            report.warnings.extend(doc.warnings)
            log.info("indexed %s [%s] -> %d chunks", p.name, doc_type, len(chunks))
        except Exception as exc:
            log.exception("failed to ingest %s", p.name)
            report.files_failed.append(p.name)
            report.warnings.append(f"failed: {p.name}: {exc}")

    manifest[bid_id] = known
    save_manifest(settings.paths.manifest, manifest)
    return report

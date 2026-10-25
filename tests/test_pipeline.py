from pathlib import Path

from app.config import Settings
from app.ingestion.pipeline import ingest_folder
from app.models import Chunk
from tests.conftest import BIDNET_LIKE_HTML, make_pdf


class MemoryStore:
    def __init__(self) -> None:
        self.chunks: dict[str, Chunk] = {}

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        self.chunks.update({c.meta.chunk_id: c for c in chunks})

    def delete_file(self, bid_id: str, file_name: str) -> None:
        self.chunks = {k: c for k, c in self.chunks.items()
                       if not (c.meta.bid_id == bid_id and c.meta.file_name == file_name)}


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "BidX"
    folder.mkdir()
    make_pdf(folder / "Main Document FINAL.pdf", ["Request For Proposal 2025-01\nScope of the devices."] * 3)
    make_pdf(folder / "Addendum 1 Main.pdf", ["ADDENDUM No. 1\nThe new due date is March 30, 2025."])
    (folder / "listing.html").write_text(BIDNET_LIKE_HTML, encoding="utf-8")
    (folder / "notes.docx").write_bytes(b"not supported")
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    return folder


def test_ingest_labels_incremental_and_errors(tmp_path: Path, settings: Settings) -> None:
    folder, store = _folder(tmp_path), MemoryStore()
    r1 = ingest_folder(folder, store, settings)
    assert r1.bid_id == "BidX"
    assert sorted(r1.files_indexed) == ["Addendum 1 Main.pdf", "Main Document FINAL.pdf", "listing.html"]
    assert r1.files_failed == ["broken.pdf"]  # one bad file does not stop the folder
    assert any("notes.docx" in w for w in r1.warnings)
    types = {c.meta.file_name: (c.meta.doc_type, c.meta.addendum_number) for c in store.chunks.values()}
    assert types["Addendum 1 Main.pdf"] == ("addendum", 1)
    assert types["Main Document FINAL.pdf"] == ("rfp", None)
    assert types["listing.html"] == ("bid_page", None)

    # second run: unchanged files are skipped
    r2 = ingest_folder(folder, store, settings)
    assert sorted(r2.files_skipped) == sorted(r1.files_indexed) and r2.chunks_added == 0

    # a changed file is re-indexed; a removed file loses its chunks
    make_pdf(folder / "Addendum 1 Main.pdf", ["ADDENDUM No. 1\nThe new due date is April 2, 2025."])
    (folder / "listing.html").unlink()
    r3 = ingest_folder(folder, store, settings)
    assert r3.files_indexed == ["Addendum 1 Main.pdf"]
    assert not any(c.meta.file_name == "listing.html" for c in store.chunks.values())
    assert any("April 2, 2025" in c.text for c in store.chunks.values())

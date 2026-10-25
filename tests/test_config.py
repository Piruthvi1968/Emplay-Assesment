"""Settings loading and shared-model sanity checks."""
from app.config import PROJECT_ROOT, load_settings
from app.models import ChunkMeta


def test_settings_load_and_paths_are_absolute() -> None:
    s = load_settings()
    assert s.search.rrf_k == 60
    assert s.paths.index_dir.is_absolute()
    assert s.paths.data_dir == PROJECT_ROOT / "data"


def test_twenty_fields_in_four_groups() -> None:
    s = load_settings()
    assert len(s.fields) == 20
    sizes = {g: len(fs) for g, fs in s.fields_by_group().items()}
    assert sizes == {"overview_dates": 7, "commercial_legal": 7, "product_specs": 5, "summary": 1}
    assert len({f.name for f in s.fields}) == 20


def test_chunk_meta_chroma_round_trip() -> None:
    meta = ChunkMeta(chunk_id="B::f::p-1::c0", bid_id="B", file_name="f.html", doc_type="bid_page")
    stored = meta.to_chroma()
    assert None not in stored.values()
    assert stored["page_number"] == -1 and stored["section"] == ""
    assert ChunkMeta.from_chroma(stored) == meta

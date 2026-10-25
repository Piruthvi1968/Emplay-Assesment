from app.config import IngestionCfg
from app.ingestion.chunker import chunk_doc, estimate_tokens, pack_units, split_table
from app.ingestion.cleaning import is_heading, join_broken_lines, normalize_chars
from app.models import Block, ParsedDoc, ParsedPage

CFG = IngestionCfg(chunk_target_tokens=100, chunk_max_tokens=150, chunk_overlap_tokens=15)


def _doc(*pages: list[Block]) -> ParsedDoc:
    return ParsedDoc(
        file_name="Addendum 1 X.pdf", pages=[ParsedPage(page_number=i + 1, blocks=b) for i, b in enumerate(pages)]
    )


def _chunks(doc: ParsedDoc):
    return chunk_doc(doc, bid_id="BidT", doc_type="addendum", addendum_number=1, doc_date=None, cfg=CFG)


def test_size_limits_and_overlap() -> None:
    sentences = [f"Sentence number {i} talks about delivery requirements in detail." for i in range(60)]
    out = pack_units(sentences, target=100, max_tokens=150, overlap=15)
    assert len(out) > 1
    assert all(estimate_tokens(c) <= 150 for c in out)
    # the start of each chunk repeats text from the end of the previous one
    assert out[1].splitlines()[0] in out[0]


def test_table_split_repeats_header() -> None:
    rows = "\n".join(f"| {i} | Laptop model {i} with long description text here | 30 |" for i in range(40))
    table = "| Line | Item | Qty |\n|---|---|---|\n" + rows
    pieces = split_table(table, max_tokens=150)
    assert len(pieces) > 1
    assert all(p.startswith("| Line | Item | Qty |\n|---|---|---|") for p in pieces)


def test_addendum_qa_kept_together_and_deterministic_ids() -> None:
    qa = "\n".join(
        f"{i}. Question {i} about the warranty period for devices?\nAnswer:\nThe answer to question {i} is three years."
        for i in range(1, 4)
    )
    doc = _doc([Block(kind="text", text=qa)])
    chunks = _chunks(doc)
    for i in range(1, 4):
        holder = [c for c in chunks if f"Question {i} about" in c.text]
        assert holder and f"answer to question {i} is" in holder[0].text
    assert [c.meta.chunk_id for c in chunks] == [c.meta.chunk_id for c in _chunks(doc)]
    assert chunks[0].meta.chunk_id == "BidT::Addendum 1 X::p1::c0"
    assert chunks[0].text.startswith("[BidT | Addendum 1 | Addendum 1 X | p.1")
    assert chunks[0].meta.addendum_number == 1


def test_chunks_never_cross_pages_and_section_carries_over() -> None:
    doc = _doc(
        [Block(kind="text", text="2.1 Delivery Terms\nAll devices must be delivered within 45 days of award.")],
        [Block(kind="text", text="Late delivery may result in cancellation of the order by the district.")],
    )
    chunks = _chunks(doc)
    assert {c.meta.page_number for c in chunks} == {1, 2}
    assert chunks[-1].meta.section == "2.1 Delivery Terms"


def test_heading_rules() -> None:
    assert is_heading("1.2 Terms")
    assert is_heading("SCOPE OF WORK")
    assert not is_heading("ALZATE, JASMINE")  # a name, not a heading
    assert not is_heading("80 Calvert Street, Room 109")
    assert not is_heading("1. The vendor shall provide all labor and materials.")


def test_cleaning_joins_fragments_and_fixes_emails() -> None:
    assert join_broken_lines("Questions\nDue\n(Closing)\nDate") == "Questions Due (Closing) Date"
    assert join_broken_lines("1.2 Terms\nInitial Term:") == "1.2 Terms\nInitial Term:"
    assert normalize_chars("vendors’ “quote”") == 'vendors\' "quote"'
    assert normalize_chars("a@treasurer.state.md .us") == "a@treasurer.state.md.us"
    assert normalize_chars("b@treasurer.state.\nmd.us") == "b@treasurer.state.md.us"

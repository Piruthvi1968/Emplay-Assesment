from app.search.bm25_index import tokenize
from app.search.engine import id_like_tokens


def test_ids_kept_whole_and_split() -> None:
    assert tokenize("Base 210-BLYZ") == ["base", "210-blyz", "210", "blyz"]
    assert tokenize("JA-207652") == ["ja-207652", "ja", "207652"]
    assert "e20p4600040" in tokenize("PORFP Number: #E20P4600040")


def test_lowercase_and_stop_words() -> None:
    assert tokenize("What is THE Due Date of the RFP?") == ["due", "date", "rfp"]


def test_id_like_tokens() -> None:
    assert id_like_tokens("What is 210-BLYZ and WD22TB4 in JA-207652?") == ["210-BLYZ", "WD22TB4", "JA-207652"]
    assert id_like_tokens("due 07/09/2024 in 2024, USB 3.1") == []

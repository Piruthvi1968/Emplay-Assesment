from app.ingestion.detect import assign_doc_types, detect_addendum_number, detect_doc_date, detect_doc_type


def test_doc_type_from_file_name() -> None:
    assert detect_doc_type("Addendum 2 RFP XYZ-1 Laptops.pdf", "") == "addendum"
    assert detect_doc_type("Contract_Affidavit.pdf", "") == "affidavit"
    assert detect_doc_type("Laptop_Specifications.pdf", "") == "specs"
    assert detect_doc_type("PORFP_-_Laptops.pdf", "") == "rfp"
    assert detect_doc_type("listing.html", "anything") == "bid_page"


def test_doc_type_from_first_page_and_fallback() -> None:
    assert detect_doc_type("XYZ-1 Devices FINAL.pdf", "PURCHASING\nRequest For Proposal 1234") == "rfp"
    assert detect_doc_type("notes.pdf", "nothing useful here") is None
    types = assign_doc_types({"a.pdf": None, "b.pdf": None, "c.html": "bid_page"}, {"a.pdf": 10, "b.pdf": 99})
    assert types == {"a.pdf": "other", "b.pdf": "rfp", "c.html": "bid_page"}
    # a folder that already has an RFP does not promote another file
    assert assign_doc_types({"a.pdf": "rfp", "b.pdf": None}, {"b.pdf": 99})["b.pdf"] == "other"


def test_addendum_number() -> None:
    assert detect_addendum_number("Addendum 2 RFP X.pdf", "") == 2
    assert detect_addendum_number("amend.pdf", "ADDENDUM No. 3\nRFP X") == 3
    assert detect_addendum_number("Addendum #12.pdf", "") == 12
    assert detect_addendum_number("rfp.pdf", "no number") is None


def test_doc_date_formats() -> None:
    assert detect_doc_date("Issued 27-JUN-2024 14:00") == "2024-06-27"
    assert detect_doc_date("The new due date will be July 9, 2024 at 2:00 PM") == "2024-07-09"
    assert detect_doc_date("Closing Date: 06/10/2024\nPublication: 05/28/2024 11:47 AM") == "2024-05-28"
    assert detect_doc_date("no dates here") is None

# Retrieval eval set: evidence notes

26 questions (14 Bid1, 10 Bid2, 2 cross-bid with `bid_id: null`). Pages are **physical 1-based PDF pages**;
`page: null` = BidNet HTML page. Every `text_contains` was checked as a substring of the page text after
normalisation (curly quotes to straight, en/em dashes to `-`, whitespace collapsed). Result: 45/45 expected
entries pass. As an extra check, each one is also found inside a chunk produced by the project's own
`parse_file` + `chunk_doc` on the same page (45/45).

Short file names used below:
- **B1-RFP** = `JA-207652 Student and Staff Computing Devices FINAL.pdf`
- **B1-A1** / **B1-A2** = `Addendum 1 ...pdf` / `Addendum 2 ...pdf`
- **B1-HTML** = Bid1 BidNet HTML
- **B2-PORFP** = `PORFP_-_Dell_Laptop_Final.pdf`
- **B2-SPECS** = `Dell_Laptop_Specs.pdf`
- **B2-MERC** = `Mercury_Affidavit.pdf`
- **B2-HTML** = Bid2 BidNet HTML

| id | type | Why it is in the set | Verified evidence (file, page: quote) |
|---|---|---|---|
| q01 | due_date_addendum | Final deadline after an addendum change (trap 1). The addendum chunk has to beat the many base-RFP date chunks | B1-A2 p1: "The new due date for this RFP will be July 9, 2024"; B1-HTML: "07/09/2024 03:00 PM EDT" (Closing Date) |
| q02 | due_date_original | The original date in the RFP header, so the base value can still be retrieved | B1-RFP p2: "27-JUN-2024 14:00:00" (after "Solicitation Due") |
| q03 | due_date | Paraphrase ("close") with no document vocabulary. Bid2 closing date | B2-PORFP p1: "PROPOSAL DUE DATE and TIME" (followed by 06/10/2024); B2-HTML: "06/10/2024 02:00 PM EDT" |
| q04 | part_number | An exact SKU, which tests BM25 tokenisation and pinning | B2-SPECS p1: "210-BLYZ" |
| q05 | identifier | Exact solicitation ID lookup with no bid filter | B2-PORFP p1: "#E20P4600040"; p2: "QUESTION for Dell Laptop #E20P4600040" |
| q06 | identifier | Hyphenated ID lookup across the RFP and both addenda | B1-RFP p1: "168884 JA-207652 Student and Staff Computing Devices"; B1-A1 p1 and B1-A2 p1: "JA-207652 Student and Staff Computing Devices" |
| q07 | contact | Buyer name and email from the RFP header | B1-RFP p2: "JALZATE@dallasisd.org" (after "Buyer ALZATE, JASMINE Email") |
| q08 | contact | POC phone, found in both the PDF and the HTML | B2-PORFP p3: "410-260-7533" (Agency POC Phone Number); B2-HTML: "410-260-7533" |
| q09 | submission | Submission method (portal preferred, sealed manual, no fax/email) | B1-RFP p9: "submitted online via our electronic system"; "No fax or email submissions will be accepted" |
| q10 | submission | Paraphrased yes/no question. The answer is a negative statement | B2-PORFP p1: "Bids will not be accepted by email, fax" |
| q11 | warranty | Warranty minimums from the Addendum 1 Q&A (Q7, repeated in Q29) plus the RFP spec bullets | B1-A1 p1 and p3: "one-year warranty for student Chromebooks"; B1-RFP p4: "Warranty: 1-year minimum"; p5: "Warranty: 3-year minimum" |
| q12 | warranty | Warranty requirement held in a PDF form table | B2-PORFP p3: "Dell Limited Hardware Warranty Extended"; "3 years following the date of delivery" |
| q13 | forms | Required affidavit: the requirement plus the form itself | B2-PORFP p2: "must provide a Mercury Affidavit"; B2-MERC p1: "MERCURY CONTENT INFORMATION" |
| q14 | forms | Required attachments listed in the RFP attachments table | B1-RFP p33: "W9 Form completed, signed and attached"; "MWBE Forms must be completed and attached" |
| q15 | delivery | Delivery window. The trap is the product-table "Due Date" column (06/10/2024), which is not delivery | B2-PORFP p2: "Delivery within 45 days of Award" |
| q16 | delivery | Paraphrase. The answer exists only in the addendum Q&A (Q4, Q15) | B1-A1 p1: "starting in September of 2024"; B1-A1 p2: "meet the deadline for the agreed delivery" |
| q17 | contract_vehicle | Master contract number | B2-PORFP p1: "Desktop, Laptop and Tablet 2015 Master Contract, 060B5400007" |
| q18 | contract_vehicle | Paraphrase ("buy off the contract") of the cooperative interlocal riders | B1-RFP p19: "Educational Purchasing Cooperative of North Texas"; p20: "Central Texas Purchasing Alliance (CTPA)" |
| q19 | specs_table | Spec values from the Dell quote (table-like content) | B2-SPECS p1: "Intel Core Ultra 5 125U processor"; "16 GB: 2 x 8 GB, DDR5" |
| q20 | bidnet_only | A fact that exists only in the BidNet HTML | B1-HTML: "00004079100" (Reference Number) |
| q21 | term | Contract term and renewals | B1-RFP p2: "may be extended not to exceed a total of 5 years" (the same block has "Initial Term: 3 Renewal 1: 1 Renewal 2: 1") |
| q22 | pre_bid | Pre-proposal meeting. The text says "highly encouraged", not mandatory | B1-RFP p2: "A pre-proposal meeting will be held at 2:00 PM CST"; B1-HTML: "06/10/2024 03:00 PM EDT" (Prebid Conference) |
| q23 | payment | Invoicing instructions (no Net term stated anywhere) | B2-PORFP p2: "STOaccountspayable@treasurer.state.md.us"; "submitted within 10 days of delivering the equipment" |
| q24 | not_found | Hallucination guard: no bid bond amount exists in Bid1. `expected: []`, so retrieval scores 0 here by design. Use this item to calibrate `not_found_threshold`, and leave it out of the Recall/MRR averages (or report it separately) | Only generic text exists: B1-RFP p9 "must comply with any insurance, bid bond, or liability requirements ... as noted elsewhere"; p17 certificate of insurance/bond clause; p52-53 M/WBE joint-venture surety text. No amount or percentage |
| q25 | addendum_change | "What changed in Addendum 2", with the addendum named in the question | B1-A2 p1: "extend the due date of this RFP" |
| q26 | multi_topic | Two topics (contact and affidavit) in one question. Tests sub-query splitting | B2-PORFP p3: "Agency POC Name: Tamaira Hawkins"; B2-HTML: "Tamaira Hawkins"; B2-PORFP p2: "must provide a Mercury Affidavit" |

## Initial ground-truth notes that differ from the documents or need care

1. **Page numbering in the Bid1 RFP.** Physical pages 1-40 carry the printed labels "Page 1 of 40" to "Page 40 of 40",
   so physical page = printed page for the first 40 pages. The two diverge only for the attached forms after p40
   (p41 onwards has M/WBE and other forms with their own numbering). The RFP header block (buyer, dates, pre-proposal,
   1.2 Terms) is on **physical p2**, not p1. Physical p1 has the RFP number and title.
2. **Cooperative (Bid1).** Besides the EPCNT interlocal rider (p19), the RFP also has a **Central Texas Purchasing
   Alliance (CTPA)** interlocal cooperative rider on p20. The initial notes listed only EPCNT.
3. **Bid2 due time.** The PORFP p1 gives only the date ("PROPOSAL DUE DATE and TIME: 06/10/2024", with no time). The
   time "02:00 PM EDT" comes only from the BidNet HTML (Closing Date).
4. **Warranty minimums (Bid1).** The Addendum 1 Q7 answer is repeated word for word as the answer to Q29 (Addendum 1 p3).
   The RFP spec bullets (p4-p6) already state "Warranty: 1-year minimum" (Chromebooks, tablet) and "3-year minimum"
   (Windows devices, desktops, monitors), so the addendum confirms them rather than changing them.
5. **Certificate of insurance "within 10 days of award" (p17).** The p17 text says "The insurance form must be received
   within ten (10)" days (the continuation was not checked word for word). It is not used in the eval set.
6. Everything else used here matched the initial notes: the Addendum 2 due date; the Addendum 1 Q11 intent statement ("does
   not anticipate extending the submission deadline", Addendum 1 p1); the IDs; the contacts; the eMMA-only submission;
   45-day delivery; 060B5400007; the Mercury Affidavit; invoicing; the questions-due date 06/01/2024 2:00 PM EDT (PORFP p2);
   the "Clavert Street" typo (PORFP p1); the FA I / FA V header vs "FA II - Printers and Associated Peripherals" scope
   (PORFP p3). No bid bond appears anywhere in Bid2 either (searched all Bid2 files for "bond").

## Added after the first eval run
- **q27** (not_found, Bid2): "When is the pre-bid conference for the Dell laptop PORFP?". No Bid2 chunk mentions pre-bid, pre-proposal or conference (checked with a regex over every Bid2 chunk). Added so the not-found threshold is calibrated on more than one question.
- A candidate question about Bid2 **installation** was dropped: the Dell quote (Dell_Laptop_Specs p.2) lists "Factory Install" SKUs (asset tag, asset report, OS validation). Installation is therefore *not* clearly absent for Bid2, which contradicts the initial ground-truth note "Installation: not mentioned".

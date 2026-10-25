# Retrieval evaluation report

Generated 2026-10-25 12:23 by `python main.py eval`. 27 questions (25 answerable; not-found questions are excluded from Recall/MRR).
Embedding `BAAI/bge-small-en-v1.5`, re-ranker `Xenova/ms-marco-MiniLM-L-12-v2`, 20 candidates per list, RRF k=60, 10 re-rank candidates. Questions are sent as-is (no LLM query planner).

## Summary

| Configuration | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR@10 | Avg latency (ms) |
|---|---|---|---|---|---|---|
| vector | 0.52 | 0.76 | 0.84 | 0.92 | 0.644 | 7 |
| hybrid | 0.52 | 0.84 | 0.88 | 1.00 | 0.695 | 7 |
| hybrid_rerank | 0.72 | 0.88 | 0.88 | 1.00 | 0.803 | 859 |

Recall@k = share of questions with at least one relevant passage in the top k. MRR = mean of 1/rank of the first relevant passage (0 if none in the top 10).

## Rank of the first relevant passage per question

| id | type | vector | hybrid | hybrid_rerank | question |
|---|---|---|---|---|---|
| q01 | due_date_addendum | 1 | 1 | 1 | What is the final proposal due date for Bid1 after the addenda? |
| q02 | due_date_original | 3 | 4 | 2 | What was the original solicitation due date and time in the Dallas ISD RFP? |
| q03 | due_date | 1 | 2 | 1 | When do proposals close for the Maryland Dell laptop purchase? |
| q04 | part_number | miss | 1 | 1 | What is 210-BLYZ? |
| q05 | identifier | 1 | 1 | 1 | Which solicitation has PORFP number E20P4600040? |
| q06 | identifier | 3 | 2 | 2 | What is RFP JA-207652 for? |
| q07 | contact | 7 | 1 | 1 | Who is the buyer for the Dallas ISD computing devices RFP and what is the buyer's email? |
| q08 | contact | 1 | 1 | 1 | What is the phone number of the agency point of contact for Bid2? |
| q09 | submission | 1 | 3 | 3 | How should proposals be submitted to Dallas ISD? |
| q10 | submission | 6 | 7 | 7 | Can I email my bid to the State Treasurer's Office for the laptop purchase? |
| q11 | warranty | 1 | 1 | 1 | What minimum warranty does Dallas ISD require for student Chromebooks and Windows laptops? |
| q12 | warranty | 4 | 6 | 1 | How long is the extended warranty on the Dell laptops? |
| q13 | forms | 3 | 2 | 1 | Is a Mercury Affidavit required for the Dell laptop bid? |
| q14 | forms | miss | 7 | 9 | Which forms must be attached to the Dallas ISD proposal submission? |
| q15 | delivery | 1 | 1 | 1 | What is the delivery requirement after award for Bid2? |
| q16 | delivery | 1 | 1 | 1 | When does Dallas ISD need the devices delivered? |
| q17 | contract_vehicle | 3 | 2 | 1 | Which master contract must vendors hold to bid on the Dell laptop PORFP? |
| q18 | contract_vehicle | 1 | 1 | 1 | Can members of a purchasing cooperative buy off the Dallas ISD contract? |
| q19 | specs_table | 2 | 3 | 1 | What processor and memory does the quoted Dell Latitude 5550 have? |
| q20 | bidnet_only | 2 | 1 | 1 | What is the BidNet reference number for the Dallas ISD computing devices solicitation? |
| q21 | term | 1 | 2 | 3 | How long is the Dallas ISD contract term, including renewals? |
| q22 | pre_bid | 1 | 1 | 1 | Is there a pre-proposal meeting for Bid1, and is attendance mandatory? |
| q23 | payment | 1 | 1 | 1 | Where and when must invoices be sent for the Treasurer's Office laptop order? |
| q24 | not_found | n/a | n/a | n/a | What is the bid bond amount for Bid1? |
| q25 | addendum_change | 1 | 1 | 6 | What changed in Addendum 2 of the Dallas ISD RFP? |
| q26 | multi_topic | 5 | 2 | 1 | For Bid2, who is the procurement contact, and which affidavit must the contractor provide? |
| q27 | not_found | n/a | n/a | n/a | When is the pre-bid conference for the Dell laptop PORFP? |

## Not-found threshold calibration (hybrid_rerank, cross-encoder logits)

- Top-1 score for answerable questions: min +1.23 / median +5.62 / max +7.99 (n=25)
- Score of the first relevant hit (answerable questions): min -1.99 / median +5.62 / max +7.99 (n=25)
- Best irrelevant hit per answerable question: min -9.17 / median +3.93 / max +6.85 (n=25)
- Top-1 score for not-found questions: min -0.46 / median +1.88 / max +4.23 (n=2)
- Suggested `search.not_found_threshold`: no clean separation on this set; keep the configured value
- Configured value: 0.0

Irrelevant hits can score high when they are on-topic (for example the RFP header next to the addendum that changed the date), so the threshold only flags questions with *no* plausible evidence.

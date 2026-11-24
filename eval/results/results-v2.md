# Evaluation — v2

`2026-11-24 12:16:15` · FakeERPClient · extraction: heuristic · policy retrieval: chromadb

| Metric | Value |
|---|---|
| Scenarios | 23 |
| **Task success rate** | **100.0%** (23/23) |
| Exception-detection rate | 100.0% (6 scenarios) |
| Avg steps per run | 8.3 |
| Avg LLM calls per run | 0.0 |
| Avg tokens in / out | 0 / 0 |
| Latency p50 / p95 | 180 ms / 260 ms |
| Total wall time | 4.2 s |

## By category

| Category | Pass | Fail |
|---|---|---|
| authority | 3 | 0 |
| duplicate detection | 2 | 0 |
| extraction | 4 | 0 |
| flow | 4 | 0 |
| master data | 2 | 0 |
| price variance | 2 | 0 |
| quantity / receipt | 4 | 0 |
| resilience | 2 | 0 |

## Every scenario

| Scenario | Expected | Actual | Rules | Exceptions | ms |
|---|---|---|---|---|---|
| happy_under_threshold | completed | completed ok | R1 | — | 260 |
| happy_over_threshold | completed | completed ok | R1 | — | 176 |
| approval_rejected | escalated | escalated ok | R1 | — | 184 |
| approval_pauses_without_answer | awaiting_approval | awaiting_approval ok | R1 | — | 178 |
| preferred_supplier_noted | completed | completed ok | R1,R2 | — | 177 |
| partial_receipt_qty_short | completed_with_exceptions | completed_with_exceptions ok | R1,R4 | qty_short | 201 |
| qty_over_receipt | completed_with_exceptions | completed_with_exceptions ok | R1,R4 | qty_over | 176 |
| qty_within_tolerance | completed | completed ok | R1 | — | 192 |
| price_variance_breach | completed_with_exceptions | completed_with_exceptions ok | R1,R3 | price_variance | 178 |
| price_variance_within_tolerance | completed | completed ok | R1 | — | 178 |
| duplicate_invoice_blocked | blocked | blocked ok | R1,R5 | duplicate_invoice | 174 |
| duplicate_invoice_different_punctuation | blocked | blocked ok | R1,R5 | duplicate_invoice | 180 |
| unknown_supplier_escalated | escalated | escalated ok | R6 | — | 0 |
| messy_email_typos_and_units | completed | completed ok | R1 | — | 187 |
| multi_item_pdf_quote | completed | completed ok | R1 | — | 309 |
| missing_quantity_needs_human | needs_human | needs_human ok | R8 | — | 0 |
| ambiguous_item_needs_human | needs_human | needs_human ok | R8 | — | 0 |
| expedite_flag | completed | completed ok | R1,R7 | — | 178 |
| run_ceiling_blocked | blocked | blocked ok | R1,R10 | — | 180 |
| missing_receipt_held | completed_with_exceptions | completed_with_exceptions ok | R1,R9 | missing_receipt | 183 |
| dry_run_writes_nothing | dry_run | dry_run ok | R1 | — | 191 |
| erp_validation_error_handled | needs_human | needs_human ok | R1 | — | 181 |
| read_back_verification_failure | needs_human | needs_human ok | R1,R8 | — | 200 |

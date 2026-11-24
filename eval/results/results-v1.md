# Evaluation — v1

`2026-11-24 12:01:37` · FakeERPClient · extraction: heuristic · policy retrieval: chromadb

| Metric | Value |
|---|---|
| Scenarios | 24 |
| **Task success rate** | **79.2%** (19/24) |
| Exception-detection rate | 66.7% (6 scenarios) |
| Avg steps per run | 7.3 |
| Avg LLM calls per run | 0.0 |
| Avg tokens in / out | 0 / 0 |
| Latency p50 / p95 | 314 ms / 563 ms |
| Total wall time | 8.2 s |

## By category

| Category | Pass | Fail |
|---|---|---|
| authority | 2 | 1 |
| duplicate detection | 2 | 0 |
| extraction | 4 | 0 |
| flow | 4 | 0 |
| master data | 2 | 0 |
| price variance | 2 | 0 |
| quantity / receipt | 1 | 3 |
| resilience | 2 | 1 |

## Every scenario

| Scenario | Expected | Actual | Rules | Exceptions | ms |
|---|---|---|---|---|---|
| happy_under_threshold | completed | completed ok | R1 | — | 496 |
| happy_over_threshold | completed | completed ok | R1 | — | 316 |
| approval_rejected | escalated | escalated **FAIL** | — | — | 1 |
| approval_pauses_without_answer | awaiting_approval | awaiting_approval ok | R1 | — | 369 |
| preferred_supplier_noted | completed | completed ok | R1,R2 | — | 285 |
| partial_receipt_qty_short | completed_with_exceptions | awaiting_approval **FAIL** | R1 | — | 277 |
| qty_over_receipt | completed_with_exceptions | awaiting_approval **FAIL** | R1 | — | 296 |
| qty_within_tolerance | completed | awaiting_approval **FAIL** | R1 | — | 320 |
| price_variance_breach | completed_with_exceptions | completed_with_exceptions ok | R1,R3 | price_variance | 314 |
| price_variance_within_tolerance | completed | completed ok | R1 | — | 301 |
| duplicate_invoice_blocked | blocked | blocked ok | R1,R5 | duplicate_invoice | 275 |
| duplicate_invoice_different_punctuation | blocked | blocked ok | R1,R5 | duplicate_invoice | 331 |
| unknown_supplier_escalated | escalated | escalated ok | R6 | — | 1 |
| messy_email_typos_and_units | completed | completed ok | R1 | — | 252 |
| multi_item_pdf_quote | completed | completed ok | R1 | — | 468 |
| missing_quantity_needs_human | needs_human | needs_human ok | R8 | — | 0 |
| ambiguous_item_needs_human | needs_human | needs_human ok | R8 | — | 0 |
| expedite_flag | completed | completed ok | R1,R7 | — | 257 |
| run_ceiling_blocked | blocked | blocked ok | R1,R10 | — | 362 |
| missing_receipt_held | completed_with_exceptions | completed_with_exceptions ok | R1,R9 | missing_receipt | 344 |
| dry_run_writes_nothing | dry_run | dry_run ok | R1 | — | 415 |
| erp_validation_error_handled | needs_human | needs_human ok | R1 | — | 489 |
| read_back_verification_failure | needs_human | needs_human ok | R1,R8 | — | 876 |
| transient_error_is_retried | completed | needs_human **FAIL** | R1 | — | 563 |

## Failures

- **approval_rejected** — expected `escalated`, got `escalated`
  - approval_required: expected True, got False
- **partial_receipt_qty_short** — expected `completed_with_exceptions`, got `awaiting_approval`
  - state: expected completed_with_exceptions, got awaiting_approval
  - rule R4 was not cited (cited: R1)
  - exceptions: expected ['qty_short'], got none
- **qty_over_receipt** — expected `completed_with_exceptions`, got `awaiting_approval`
  - state: expected completed_with_exceptions, got awaiting_approval
  - rule R4 was not cited (cited: R1)
  - exceptions: expected ['qty_over'], got none
- **qty_within_tolerance** — expected `completed`, got `awaiting_approval`
  - state: expected completed, got awaiting_approval
- **transient_error_is_retried** — expected `completed`, got `needs_human`
  - state: expected completed, got needs_human

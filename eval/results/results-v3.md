# Evaluation — v3

`2026-11-24 13:35:56` · FakeERPClient · extraction: heuristic · policy retrieval: chromadb

| Metric | Value |
|---|---|
| Scenarios | 48 |
| **Task success rate** | **100.0%** (48/48) |
| Exception-detection rate | 100.0% (11 scenarios) |
| **False-touchless** (should have stopped, did not) | **0** of 33 |
| Trace-invariant violations | 0 |
| Avg steps per run | 7.8 |
| Avg LLM calls per run | 0.0 |
| Avg tokens in / out | 0 / 0 |
| Latency p50 / p95 | 196 ms / 280 ms |
| Total wall time | 9.6 s |

## By category

| Category | Pass | Fail |
|---|---|---|
| authority | 9 | 0 |
| boundaries | 4 | 0 |
| duplicate detection | 4 | 0 |
| extraction | 4 | 0 |
| flow | 4 | 0 |
| master data | 5 | 0 |
| price variance | 3 | 0 |
| quantity / receipt | 8 | 0 |
| resilience | 4 | 0 |
| untrusted input | 3 | 0 |

## Every scenario

| Scenario | Expected | Actual | Rules | Exceptions | ms |
|---|---|---|---|---|---|
| happy_under_threshold | completed | completed ok | R1 | — | 267 |
| happy_over_threshold | completed | completed ok | R1,R12 | — | 167 |
| approval_rejected | escalated | escalated ok | R1 | — | 164 |
| approval_pauses_without_answer | awaiting_approval | awaiting_approval ok | R1 | — | 196 |
| preferred_supplier_noted | completed | completed ok | R1,R2 | — | 173 |
| partial_receipt_qty_short | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12,R4 | billed_over_received,qty_short | 203 |
| qty_over_receipt | completed_with_exceptions | completed_with_exceptions ok | R1,R12,R4 | qty_over | 241 |
| qty_within_tolerance | completed | completed ok | R1,R12 | — | 187 |
| price_variance_breach | completed_with_exceptions | completed_with_exceptions ok | R1,R3 | price_variance | 215 |
| price_variance_within_tolerance | completed | completed ok | R1 | — | 179 |
| duplicate_invoice_blocked | blocked | blocked ok | R1,R5 | duplicate_invoice | 219 |
| duplicate_invoice_different_punctuation | blocked | blocked ok | R1,R5 | duplicate_invoice | 197 |
| unknown_supplier_escalated | escalated | escalated ok | R6 | — | 9 |
| messy_email_typos_and_units | completed | completed ok | R1,R14 | — | 218 |
| multi_item_pdf_quote | completed | completed ok | R1,R12 | — | 344 |
| missing_quantity_needs_human | needs_human | needs_human ok | R8 | — | 8 |
| ambiguous_item_needs_human | needs_human | needs_human ok | R8 | — | 9 |
| expedite_flag | completed | completed ok | R1,R7 | — | 164 |
| run_ceiling_blocked | blocked | blocked ok | R1,R10 | — | 195 |
| missing_receipt_held | completed_with_exceptions | completed_with_exceptions ok | R1,R9 | missing_receipt | 181 |
| dry_run_writes_nothing | dry_run | dry_run ok | R1 | — | 187 |
| erp_validation_error_handled | needs_human | needs_human ok | R1 | — | 218 |
| read_back_verification_failure | needs_human | needs_human ok | R1,R8 | — | 198 |
| unordered_invoice_line | completed_with_exceptions | completed_with_exceptions ok | R1,R11 | unordered_line | 176 |
| billed_over_received | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12 | billed_over_received | 172 |
| timeout_after_commit_exactly_once | completed | completed ok | R1 | — | 194 |
| lost_submit_response_settled_by_read_back | completed | completed ok | R1 | — | 199 |
| approval_by_requester_refused | awaiting_approval | awaiting_approval ok | R1,R12 | — | 244 |
| approval_beyond_authority_refused | awaiting_approval | awaiting_approval ok | R1,R12 | — | 222 |
| approval_by_head_of_finance_completes | completed | completed ok | R1,R12 | — | 274 |
| order_edited_while_awaiting_approval_is_not_submitted | blocked | blocked ok | R1,R12 | — | 205 |
| hidden_instruction_in_quote_pdf_quarantined | needs_human | needs_human ok | R13 | — | 17 |
| prompt_injection_in_email_quarantined | needs_human | needs_human ok | R13 | — | 5 |
| bank_detail_change_request_escalated | escalated | escalated ok | R13 | — | 6 |
| unpriced_order_is_valued_from_the_price_list | blocked | blocked ok | R1,R10,R14 | — | 191 |
| quoted_above_price_list_needs_approval | awaiting_approval | awaiting_approval ok | R14 | — | 180 |
| split_order_needs_approval | awaiting_approval | awaiting_approval ok | R15 | — | 176 |
| lookalike_invoice_number_held | completed_with_exceptions | completed_with_exceptions ok | R1,R5 | possible_duplicate | 251 |
| same_amount_new_number_held | completed_with_exceptions | completed_with_exceptions ok | R1,R5 | possible_duplicate | 218 |
| one_rod_short_billed_in_full | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12 | billed_over_received | 221 |
| supplier_on_hold_escalates | escalated | escalated ok | R6 | — | 8 |
| forged_quote_with_another_gstin_stopped | needs_human | needs_human ok | R6 | — | 18 |
| payments_only_hold_still_orders | completed | completed ok | R1,R6 | — | 208 |
| r1_exactly_at_threshold_is_automatic | completed | completed ok | R1 | — | 220 |
| r1_one_paisa_over_needs_approval | awaiting_approval | awaiting_approval ok | R1 | — | 292 |
| r10_exactly_at_ceiling_is_allowed | completed | completed ok | R1,R12 | — | 252 |
| r10_one_paisa_over_is_blocked | blocked | blocked ok | R1,R10 | — | 280 |
| over_receipt_refused_by_erpnext_default | needs_human | needs_human ok | R1,R12 | — | 266 |

# Evaluation — v3

`2026-11-24 13:59:07` · FakeERPClient · extraction: heuristic · policy retrieval: chromadb

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
| Latency p50 / p95 | 172 ms / 217 ms |
| Total wall time | 8.3 s |

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
| happy_under_threshold | completed | completed ok | R1 | — | 242 |
| happy_over_threshold | completed | completed ok | R1,R12 | — | 189 |
| approval_rejected | escalated | escalated ok | R1 | — | 156 |
| approval_pauses_without_answer | awaiting_approval | awaiting_approval ok | R1 | — | 165 |
| preferred_supplier_noted | completed | completed ok | R1,R2 | — | 166 |
| partial_receipt_qty_short | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12,R16,R4 | billed_over_received,qty_short | 176 |
| qty_over_receipt | completed_with_exceptions | completed_with_exceptions ok | R1,R12,R16,R4 | qty_over | 153 |
| qty_within_tolerance | completed | completed ok | R1,R12 | — | 169 |
| price_variance_breach | completed_with_exceptions | completed_with_exceptions ok | R1,R16,R3 | price_variance | 186 |
| price_variance_within_tolerance | completed | completed ok | R1 | — | 170 |
| duplicate_invoice_blocked | blocked | blocked ok | R1,R5 | duplicate_invoice | 173 |
| duplicate_invoice_different_punctuation | blocked | blocked ok | R1,R5 | duplicate_invoice | 186 |
| unknown_supplier_escalated | escalated | escalated ok | R6 | — | 6 |
| messy_email_typos_and_units | completed | completed ok | R1,R14 | — | 176 |
| multi_item_pdf_quote | completed | completed ok | R1,R12 | — | 290 |
| missing_quantity_needs_human | needs_human | needs_human ok | R8 | — | 6 |
| ambiguous_item_needs_human | needs_human | needs_human ok | R8 | — | 6 |
| expedite_flag | completed | completed ok | R1,R7 | — | 171 |
| run_ceiling_blocked | blocked | blocked ok | R1,R10 | — | 189 |
| missing_receipt_held | completed_with_exceptions | completed_with_exceptions ok | R1,R16,R9 | missing_receipt | 186 |
| dry_run_writes_nothing | dry_run | dry_run ok | R1 | — | 204 |
| erp_validation_error_handled | needs_human | needs_human ok | R1 | — | 172 |
| read_back_verification_failure | needs_human | needs_human ok | R1,R8 | — | 179 |
| unordered_invoice_line | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R16 | unordered_line | 177 |
| billed_over_received | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12,R16 | billed_over_received | 217 |
| timeout_after_commit_exactly_once | completed | completed ok | R1 | — | 172 |
| lost_submit_response_settled_by_read_back | completed | completed ok | R1 | — | 176 |
| approval_by_requester_refused | awaiting_approval | awaiting_approval ok | R1,R12 | — | 172 |
| approval_beyond_authority_refused | awaiting_approval | awaiting_approval ok | R1,R12 | — | 186 |
| approval_by_head_of_finance_completes | completed | completed ok | R1,R12 | — | 164 |
| order_edited_while_awaiting_approval_is_not_submitted | blocked | blocked ok | R1,R12 | — | 184 |
| hidden_instruction_in_quote_pdf_quarantined | needs_human | needs_human ok | R13 | — | 12 |
| prompt_injection_in_email_quarantined | needs_human | needs_human ok | R13 | — | 4 |
| bank_detail_change_request_escalated | escalated | escalated ok | R13 | — | 4 |
| unpriced_order_is_valued_from_the_price_list | blocked | blocked ok | R1,R10,R14 | — | 168 |
| quoted_above_price_list_needs_approval | awaiting_approval | awaiting_approval ok | R14 | — | 169 |
| split_order_needs_approval | awaiting_approval | awaiting_approval ok | R15 | — | 163 |
| lookalike_invoice_number_held | completed_with_exceptions | completed_with_exceptions ok | R1,R16,R5 | possible_duplicate | 203 |
| same_amount_new_number_held | completed_with_exceptions | completed_with_exceptions ok | R1,R16,R5 | possible_duplicate | 216 |
| one_rod_short_billed_in_full | completed_with_exceptions | completed_with_exceptions ok | R1,R11,R12,R16 | billed_over_received | 178 |
| supplier_on_hold_escalates | escalated | escalated ok | R6 | — | 5 |
| forged_quote_with_another_gstin_stopped | needs_human | needs_human ok | R6 | — | 16 |
| payments_only_hold_still_orders | completed | completed ok | R1,R6 | — | 153 |
| r1_exactly_at_threshold_is_automatic | completed | completed ok | R1 | — | 170 |
| r1_one_paisa_over_needs_approval | awaiting_approval | awaiting_approval ok | R1 | — | 202 |
| r10_exactly_at_ceiling_is_allowed | completed | completed ok | R1,R12 | — | 185 |
| r10_one_paisa_over_is_blocked | blocked | blocked ok | R1,R10 | — | 172 |
| over_receipt_refused_by_erpnext_default | needs_human | needs_human ok | R1,R12 | — | 183 |

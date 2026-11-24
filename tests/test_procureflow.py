"""Offline tests. No network, no credentials, no ERP, under thirty seconds.

Every test here runs against `FakeERPClient` or a stubbed transport, which
is what makes CI meaningful: a suite that needs a live ERPNext is a suite
that gets skipped, and a skipped suite protects nothing.

The tests are grouped by what they defend:

- **Money and tolerances** — the arithmetic that decides whether an invoice
  is paid. Both directions of R3 and R4, because the two rules combine
  their limits differently on purpose and inverting either is an easy and
  invisible mistake.
- **Policy** — that the thresholds come from the document and that the
  document still parses. A reworded policy silently reverting to defaults
  would be the worst failure in this repository.
- **The gate** — that submission is unreachable without approval, asserted
  by driving the graph rather than by reading it.
- **Verification** — that a document stored differently from what was sent
  is caught and is not folded into a generic error.
- **Resilience** — that a 5xx is retried and a 417 is not, against a
  stubbed transport rather than a live server.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from procureflow.config import PurchaseRequest, RequestLine, RunState
from procureflow.erp.client import (
    ALLOWED_READ,
    ALLOWED_WRITE,
    Doc,
    ERPError,
    ERPNextClient,
    VerificationError,
    _compare,
)
from procureflow.erp.fake import FakeERPClient, demo_company, looks_like_series
from procureflow.extract import Catalogue, heuristic_extract, read_source, resolve_items
from procureflow.graph import GATE_PREDECESSOR, NODES, Agent, RunPlan
from procureflow.match import (
    ExceptionType,
    Tolerances,
    find_duplicate_invoice,
    normalise_invoice_no,
    paise,
    rupees,
    three_way_match,
)
from procureflow.policy_rag import (
    ALWAYS_APPLICABLE,
    PolicyIndex,
    parse_policy,
    read_numbers,
    verify_numbers,
)
from procureflow.report_excel import write_exception_report
from procureflow.tools import describe_permissions
from procureflow.tracing import MASK, Tracer, redact

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 24)


@pytest.fixture
def erp() -> FakeERPClient:
    return demo_company()


@pytest.fixture(scope="session")
def policy() -> PolicyIndex:
    # Lexical, not vectors: deterministic, and CI must not download a model.
    return PolicyIndex(prefer_vectors=False)


@pytest.fixture
def agent(erp: FakeERPClient, policy: PolicyIndex) -> Agent:
    return Agent(erp, policy=policy)


def sample(name: str) -> str:
    return str(ROOT / "seed" / "samples" / name)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (1240, 124000), (1240.00, 124000), ("1,240.00", 124000),
    ("₹ 1,240.00", 124000), ("0.01", 1), (None, 0), ("", 0), ("-50.5", -5050),
])
def test_paise_parses_every_shape_a_reader_returns(raw, want):
    assert paise(raw) == want


def test_paise_rounds_half_away_from_zero_like_an_invoice():
    """Python rounds 0.005 to even; an invoice does not."""
    assert paise("0.125") == 13
    assert paise("0.135") == 14


@pytest.mark.parametrize("p,want", [
    (0, "0.00"), (124000, "1,240.00"), (4256260, "42,562.60"),
    (100000000, "10,00,000.00"), (-124000, "-1,240.00"),
])
def test_rupees_uses_indian_grouping(p, want):
    assert rupees(p) == want


def test_rupees_groups_in_lakhs_not_thousands():
    """12,34,567.89 and never 1,234,567.89."""
    assert rupees(123456789) == "12,34,567.89"


# ---------------------------------------------------------------------------
# Tolerances: R3 and R4 combine their limits differently, on purpose
# ---------------------------------------------------------------------------

def test_price_tolerance_takes_the_tighter_of_percent_and_cash():
    tol = Tolerances()
    # 5% of Rs100 is Rs5, well under the Rs500 cap, so the percentage binds.
    assert tol.price_limit(paise(100)) == paise(5)
    # 5% of Rs18,650 is Rs932.50, so the Rs500 cash cap binds instead.
    assert tol.price_limit(paise(18650)) == paise(500)


def test_quantity_tolerance_takes_the_greater_of_percent_and_unit():
    tol = Tolerances()
    # 2% of 10 is 0.2, so the 1-unit floor governs.
    assert tol.qty_limit(10) == 1.0
    # 2% of 500 is 10, which is larger than 1 unit.
    assert tol.qty_limit(500) == 10.0


def test_the_two_rules_are_not_accidentally_the_same():
    """If both used `min` or both used `max`, one of them would be wrong.

    At the small end the price rule must be *tighter* than a flat cap and
    the quantity rule must be *looser* than a flat percentage. This asserts
    they genuinely diverge rather than happening to agree on the fixtures.
    """
    tol = Tolerances()
    assert tol.price_limit(paise(100)) < tol.price_abs_paise
    assert tol.qty_limit(10) > 10 * tol.qty_pct / 100


# ---------------------------------------------------------------------------
# Three-way match
# ---------------------------------------------------------------------------

def _doc(name: str, items: list[tuple[str, float, float]], doctype="Purchase Order") -> Doc:
    return Doc(doctype, name, {"name": name, "items": [
        {"item_code": c, "qty": q, "rate": r, "uom": "Nos"} for c, q, r in items]})


def test_a_clean_three_way_match_raises_nothing():
    po = _doc("PO-1", [("A", 10, 100.0)])
    res = three_way_match(po, _doc("GRN-1", [("A", 10, 100.0)], "Purchase Receipt"),
                          _doc("INV-1", [("A", 10, 100.0)], "Purchase Invoice"))
    assert res.matched and res.exceptions == []


def test_quantity_is_checked_against_the_receipt_not_the_invoice():
    """The distinction the whole exercise turns on.

    A supplier ships 45 of 60 and invoices for 60. An order-to-invoice
    check passes perfectly; this must not.
    """
    po = _doc("PO-1", [("A", 60, 100.0)])
    grn = _doc("GRN-1", [("A", 45, 100.0)], "Purchase Receipt")
    inv = _doc("INV-1", [("A", 60, 100.0)], "Purchase Invoice")
    res = three_way_match(po, grn, inv)
    assert ExceptionType.QTY_SHORT.value in res.types
    assert "R4" in res.rules_cited


def test_over_receipt_is_also_an_exception():
    po = _doc("PO-1", [("A", 60, 100.0)])
    grn = _doc("GRN-1", [("A", 70, 100.0)], "Purchase Receipt")
    res = three_way_match(po, grn, _doc("INV-1", [("A", 60, 100.0)], "Purchase Invoice"))
    assert ExceptionType.QTY_OVER.value in res.types


def test_quantity_inside_the_band_is_not_an_exception():
    po = _doc("PO-1", [("A", 60, 100.0)])
    grn = _doc("GRN-1", [("A", 59, 100.0)], "Purchase Receipt")
    res = three_way_match(po, grn, _doc("INV-1", [("A", 60, 100.0)], "Purchase Invoice"))
    assert res.matched


def test_price_above_tolerance_is_an_exception_and_cites_r3():
    po = _doc("PO-1", [("A", 1, 4320.0)])
    inv = _doc("INV-1", [("A", 1, 4700.0)], "Purchase Invoice")
    res = three_way_match(po, _doc("GRN-1", [("A", 1, 4320.0)], "Purchase Receipt"), inv)
    assert ExceptionType.PRICE_VARIANCE.value in res.types
    assert "R3" in res.rules_cited


def test_under_billing_is_never_an_exception():
    """A supplier charging less than agreed is not a control failure."""
    po = _doc("PO-1", [("A", 1, 4320.0)])
    inv = _doc("INV-1", [("A", 1, 3000.0)], "Purchase Invoice")
    res = three_way_match(po, _doc("GRN-1", [("A", 1, 4320.0)], "Purchase Receipt"), inv)
    assert res.matched


def test_a_missing_receipt_is_held_under_r9():
    po = _doc("PO-1", [("A", 10, 100.0)])
    res = three_way_match(po, None, _doc("INV-1", [("A", 10, 100.0)], "Purchase Invoice"))
    assert ExceptionType.MISSING_RECEIPT.value in res.types
    assert "R9" in res.rules_cited


def test_the_same_item_on_two_po_lines_is_aggregated_not_double_counted():
    """Two lines of the same item with different schedule dates is ordinary.

    Matching line-by-line reports a phantom shortfall on one and an overage
    on the other, and the net is zero.
    """
    po = _doc("PO-1", [("A", 30, 100.0), ("A", 30, 100.0)])
    grn = _doc("GRN-1", [("A", 60, 100.0)], "Purchase Receipt")
    res = three_way_match(po, grn, _doc("INV-1", [("A", 60, 100.0)], "Purchase Invoice"))
    assert res.matched, res.exceptions


def test_a_duplicate_short_circuits_every_other_check():
    """R8: once R5 fires, nothing else about the document matters."""
    po = _doc("PO-1", [("A", 60, 100.0)])
    grn = _doc("GRN-1", [("A", 10, 100.0)], "Purchase Receipt")   # also qty_short
    res = three_way_match(po, grn, _doc("INV-1", [("A", 60, 999.0)], "Purchase Invoice"),
                          duplicate_of={"name": "ACC-PINV-2026-00001"})
    assert res.types == {ExceptionType.DUPLICATE_INVOICE.value}


# ---------------------------------------------------------------------------
# R5 normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("INV-0042", "inv 0042"), ("INV-0042", "INV0042"), ("inv/0042", "INV 0042"),
])
def test_invoice_numbers_differing_only_in_punctuation_are_one_invoice(a, b):
    assert normalise_invoice_no(a) == normalise_invoice_no(b)


def test_different_invoice_numbers_stay_different():
    assert normalise_invoice_no("INV-0042") != normalise_invoice_no("INV-0043")


def test_duplicate_detection_is_scoped_to_the_supplier():
    """The same number from two suppliers is two invoices, not a duplicate."""
    ledger = [{"name": "PI-1", "supplier": "A Ltd", "bill_no": "INV-1"}]
    assert find_duplicate_invoice("A Ltd", "inv 1", ledger) is not None
    assert find_duplicate_invoice("B Ltd", "inv 1", ledger) is None


def test_an_empty_invoice_number_never_matches_anything():
    ledger = [{"name": "PI-1", "supplier": "A Ltd", "bill_no": ""}]
    assert find_duplicate_invoice("A Ltd", "", ledger) is None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def test_every_rule_in_the_policy_parses():
    rules = parse_policy()
    assert [r.rule_id for r in rules] == [f"R{i}" for i in range(1, 11)]
    assert all(r.title and r.text for r in rules)


def test_the_shipped_policy_still_yields_its_documented_thresholds():
    """A reworded policy silently reverting to defaults is the worst failure here."""
    n = read_numbers()
    assert verify_numbers(n) == []
    assert n.approval_ceiling_paise == paise(50_000)
    assert n.run_ceiling_paise == paise(5_00_000)
    assert n.price_pct == 5.0 and n.price_abs_paise == paise(500)
    assert n.qty_pct == 2.0 and n.qty_abs_units == 1.0


def test_preferred_suppliers_parse_as_company_names_not_markup():
    """Bold is used for emphasis as well as names; 'not' and 'Why.' are not suppliers."""
    n = read_numbers()
    assert n.preferred == ("Bharat Fasteners Pvt Ltd", "Deccan Steel & Alloys")


def test_verify_numbers_rejects_a_bad_parse():
    n = read_numbers()
    n.preferred = ("not", "Why.")
    assert verify_numbers(n)


def test_authority_rules_are_returned_even_when_retrieval_misses_them(policy):
    """A vector search must not be able to switch off a control."""
    got = {r.rule_id for r in policy.applicable("hex bolts for the stores", k=2)}
    assert set(ALWAYS_APPLICABLE).issubset(got)


def test_retrieval_finds_the_right_rule_for_an_obvious_query(policy):
    assert "R6" in {r.rule_id for r in policy.retrieve("supplier not in the master", 3)}


# ---------------------------------------------------------------------------
# Extraction and resolution
# ---------------------------------------------------------------------------

def test_a_full_company_name_survives_extraction():
    """The suffix list contains 'Fasteners', so a non-greedy match truncated
    'Bharat Fasteners Pvt Ltd' to its first two words."""
    req = heuristic_extract(Path(sample("req-happy.txt")).read_text(encoding="utf-8"))
    assert req.supplier == "Bharat Fasteners Pvt Ltd"


def test_a_supplier_on_its_own_line_is_found():
    req = heuristic_extract(Path(sample("req-expedite.txt")).read_text(encoding="utf-8"))
    assert req.supplier == "Bharat Fasteners Pvt Ltd"


def test_the_capture_does_not_run_across_a_blank_line():
    req = heuristic_extract(
        Path(sample("req-unknown-supplier.txt")).read_text(encoding="utf-8"))
    assert req.supplier == "Vindhya Tooling Solutions Pvt Ltd"


def test_two_items_in_one_sentence_are_both_found():
    req = heuristic_extract(
        Path(sample("req-two-in-one-line.txt")).read_text(encoding="utf-8"))
    assert len(req.items) == 2


def test_a_metadata_label_is_not_read_as_an_item():
    """'Date: 2026-11-20' parsed as an item with a quantity of 2026."""
    req = heuristic_extract("Supplier: Deccan Steel & Alloys\nDate: 2026-11-20\n"
                            "Valid until: 2026-12-31\n")
    assert req.items == []


def test_a_pdf_table_is_read_as_rows_not_as_cells():
    """pypdf flattens the table to one cell per line, so the UOM column was
    being read as an item description."""
    text, kind = read_source(sample("quote-deccan-multi.pdf"))
    req = heuristic_extract(text, kind=kind)
    assert req.supplier == "Deccan Steel & Alloys"
    assert len(req.items) == 3
    assert all(i.qty and i.rate for i in req.items)
    assert "Nos" not in [i.description for i in req.items]


def test_a_missing_quantity_is_never_assumed(erp):
    req = heuristic_extract(Path(sample("req-noqty.txt")).read_text(encoding="utf-8"))
    assert not req.is_complete
    assert any("quantity" in m or "items" in m for m in req.missing())


def test_resolution_refuses_an_ambiguous_item(erp):
    """'hex bolt' matches M8 and M10 about equally. Picking one is a coin flip."""
    req = PurchaseRequest(items=[RequestLine(description="hex bolt", qty=10)])
    notes = resolve_items(req, Catalogue.from_erp(erp))
    assert req.items[0].item_code is None
    assert any("too close to call" in n for n in notes)


def test_resolution_accepts_a_clear_fuzzy_match(erp):
    req = PurchaseRequest(items=[RequestLine(description="hex bolt m10x50 stainles",
                                             qty=10)])
    resolve_items(req, Catalogue.from_erp(erp))
    assert req.items[0].item_code == "FAS-M10-HEX"
    assert req.items[0].match_method == "fuzzy"


def test_the_model_never_supplies_an_item_code():
    """Resolution happens against the live master, not from the model."""
    schema = PurchaseRequest.model_json_schema()
    line = schema["$defs"]["RequestLine"]["properties"]
    assert "item_code" in line          # present on the model...
    from procureflow.extract import _schema
    assert "item_code" not in _schema()["$defs"]["RequestLine"]["properties"]


def test_an_iso_date_is_required_and_a_vague_one_becomes_none():
    assert PurchaseRequest(needed_by="2026-12-15").needed_by == "2026-12-15"
    assert PurchaseRequest(needed_by="next Tuesday").needed_by is None


def test_expedite_is_calculated_from_the_request_date():
    r = PurchaseRequest(needed_by="2026-11-25")
    assert r.needs_expedite(date(2026, 11, 24))
    assert not r.needs_expedite(date(2026, 11, 1))


# ---------------------------------------------------------------------------
# The ERP boundary
# ---------------------------------------------------------------------------

def test_the_fake_names_documents_with_a_naming_series(erp):
    doc = erp.insert("Material Request", {
        "material_request_type": "Purchase", "schedule_date": "2026-12-01",
        "supplier": "Bharat Fasteners Pvt Ltd",
        "items": [{"item_code": "FAS-M8-HEX", "qty": 10, "rate": 12}]})
    assert looks_like_series(doc.name)
    assert doc.name.startswith("MAT-MR-")


def test_the_server_recomputes_amount_and_the_payload_is_not_trusted(erp):
    doc = erp.insert("Material Request", {
        "material_request_type": "Purchase", "schedule_date": "2026-12-01",
        "supplier": "Bharat Fasteners Pvt Ltd",
        "items": [{"item_code": "FAS-M8-HEX", "qty": 10, "rate": 12, "amount": 99999}]})
    assert doc.items[0]["amount"] == 120.0


def test_read_back_verification_catches_a_document_stored_differently(erp):
    erp.seed("Supplier", "Bharat Fasteners Pvt Ltd", supplier_name="x")
    with pytest.raises(VerificationError):
        erp.insert("Material Request", {
            "material_request_type": "Purchase", "schedule_date": "2026-12-01",
            "supplier": "Bharat Fasteners Pvt Ltd",
            "items": [{"item_code": "FAS-M8-HEX", "qty": 10, "rate": 12}]},
            verify={"supplier": "Someone Else"})


def test_compare_tolerates_float_formatting_but_not_a_changed_string():
    assert _compare({"qty": 10}, {"qty": 10.0}) == []
    assert _compare({"supplier": "A Ltd"}, {"supplier": "B Ltd"})


def test_a_mandatory_field_omission_is_a_parsed_error_not_a_crash(erp):
    with pytest.raises(ERPError) as e:
        erp.insert("Purchase Order", {"supplier": "Bharat Fasteners Pvt Ltd"})
    assert "mandatory" in str(e.value).lower()


def test_a_doctype_outside_the_allowlist_is_refused(erp):
    with pytest.raises(ERPError):
        erp.insert("User", {"email": "x@y.z"})
    with pytest.raises(ERPError):
        erp.get_doc("Payment Entry", "PE-1")


def test_the_application_cannot_write_a_supplier():
    assert "Supplier" in ALLOWED_READ
    assert "Supplier" not in ALLOWED_WRITE
    assert describe_permissions()["can_create_supplier"] is False
    assert describe_permissions()["raw_http"] is False


def test_a_draft_order_cannot_be_received_against(erp):
    erp.seed("Purchase Order", "PUR-ORD-2026-00001", supplier="Bharat Fasteners Pvt Ltd",
             docstatus=0, items=[])
    with pytest.raises(ERPError):
        erp.run_method("erpnext.buying.doctype.purchase_order.purchase_order."
                       "make_purchase_receipt", source_name="PUR-ORD-2026-00001")


# ---------------------------------------------------------------------------
# Transport: retried and not-retried failures
# ---------------------------------------------------------------------------

class _Transport:
    """A stubbed httpx transport, so retry policy is tested without a server."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def handle_request(self, request):                           # noqa: ANN001
        import httpx
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        body = {"data": {"name": "MAT-MR-2026-00001", "docstatus": 0}}
        return httpx.Response(status, json=body, request=request)


def _client_with(statuses: list[int]) -> tuple[ERPNextClient, _Transport]:
    import httpx
    t = _Transport(statuses)
    c = ERPNextClient("http://erp.invalid", "k", "s", max_retries=3)
    c._client = httpx.Client(base_url="http://erp.invalid", transport=t,
                             headers={"Authorization": "token k:s"})
    return c, t


def test_a_server_error_is_retried():
    c, t = _client_with([500, 500, 200])
    assert c.get_doc("Material Request", "MAT-MR-2026-00001").name
    assert t.calls == 3


def test_a_validation_error_is_not_retried():
    """A 417 means the document is wrong. Sending it twice more only puts
    three identical complaints in the log."""
    c, t = _client_with([417])
    with pytest.raises(ERPError):
        c.get_doc("Material Request", "MAT-MR-2026-00001")
    assert t.calls == 1


def test_a_server_message_is_unpacked_into_one_readable_sentence():
    import httpx
    msg = json.dumps([json.dumps({"message": "<b>Schedule Date</b> cannot be before "
                                             "Transaction Date"})])

    class T:
        def handle_request(self, request):                       # noqa: ANN001
            return httpx.Response(417, json={"_server_messages": msg}, request=request)

    c = ERPNextClient("http://erp.invalid", "k", "s", max_retries=1)
    c._client = httpx.Client(base_url="http://erp.invalid", transport=T())
    with pytest.raises(ERPError) as e:
        c.get_doc("Material Request", "X")
    assert "Schedule Date cannot be before" in str(e.value)
    assert "<b>" not in str(e.value)


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

def test_the_happy_path_creates_all_four_documents(agent):
    s, tr = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.COMPLETED
    assert all([s.mr, s.po, s.receipt, s.invoice])
    assert s.po.docstatus == 1
    assert len(tr.steps) == len(NODES)


def test_submit_is_the_node_after_the_gate_and_only_after_it():
    """Asserted against the node order, not by reading the code and hoping."""
    i = NODES.index("submit_purchase_order")
    assert NODES[i - 1] == GATE_PREDECESSOR["submit_purchase_order"] == "approval_gate"


def test_an_order_over_the_ceiling_stops_at_the_gate(agent):
    s, _ = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    assert s.state is RunState.AWAITING_APPROVAL
    assert s.needs_approval
    assert s.po is not None and s.po.docstatus == 0


def test_a_rejected_approval_never_submits(agent):
    """The bug the eval found: a pre-set rejection escalated before the gate
    ran, so policy never evaluated and submit was reached anyway."""
    s, _ = agent.run(sample("req-over-threshold.txt"), approve=False, today=TODAY)
    assert s.state is RunState.ESCALATED
    assert s.needs_approval is True
    assert s.po.docstatus == 0


def test_an_approved_order_submits_and_completes(agent):
    s, _ = agent.run(sample("req-over-threshold.txt"), approve=True, today=TODAY)
    assert s.state is RunState.COMPLETED
    assert s.po.docstatus == 1


def test_resuming_a_paused_run_submits_it(agent):
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    assert s.state is RunState.AWAITING_APPROVAL
    s2, _ = agent.resume(s, tr, approved=True, note="ok, line 2 changeover")
    assert s2.state is RunState.COMPLETED
    assert s2.po.docstatus == 1


def test_an_unknown_supplier_escalates_and_writes_nothing(agent, erp):
    s, _ = agent.run(sample("req-unknown-supplier.txt"), today=TODAY)
    assert s.state is RunState.ESCALATED
    assert "R6" in s.rules
    assert s.mr is None and s.po is None
    assert not [c for c in erp.calls if c[0] in ("insert", "submit")]


def test_the_run_ceiling_cannot_be_approved_away(agent):
    """R10 bounds what a defect can do; approval does not waive it."""
    s, _ = agent.run(sample("req-ceiling-breach.txt"), approve=True, today=TODAY)
    assert s.state is RunState.BLOCKED
    assert "R10" in s.rules
    assert s.po is None


def test_a_dry_run_writes_absolutely_nothing(agent, erp):
    s, _ = agent.run(sample("req-happy.txt"), dry_run=True, today=TODAY)
    assert s.state is RunState.DRY_RUN
    assert not [c for c in erp.calls if c[0] in ("insert", "submit")]
    assert s.po is not None and s.po.name.startswith("DRYRUN-")


def test_a_duplicate_invoice_is_blocked_on_the_second_run(agent):
    plan = RunPlan(supplier_invoice_no="BFPL/2026/0042")
    first, _ = agent.run(sample("req-happy.txt"), plan=plan, today=TODAY)
    assert first.state is RunState.COMPLETED
    second, _ = agent.run(sample("req-happy.txt"),
                          plan=RunPlan(supplier_invoice_no="bfpl 2026 0042"),
                          today=TODAY)
    assert second.state is RunState.BLOCKED
    assert "R5" in second.rules


def test_an_erp_error_becomes_a_stated_reason_not_a_stack_trace(agent, erp):
    erp.fail_next(ERPError("Schedule Date cannot be before Transaction Date",
                           status=417), doctype="Material Request", op="insert")
    s, _ = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.NEEDS_HUMAN
    assert "Schedule Date" in s.reason


def test_every_node_records_a_trace_step(agent):
    s, tr = agent.run(sample("req-happy.txt"), today=TODAY)
    assert [x.node for x in tr.steps] == list(NODES)
    assert all(x.outcome for x in tr.steps)


def test_a_policy_change_moves_the_decision_with_no_code_change(erp, policy):
    """The property an implementation consultant is actually selling."""
    from procureflow.policy_rag import read_numbers as rn
    lowered = rn(policy.rules)
    lowered.approval_ceiling_paise = paise(1_000)
    a = Agent(erp, policy=policy, numbers=lowered)
    s, _ = a.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.AWAITING_APPROVAL


# ---------------------------------------------------------------------------
# Tracing and reporting
# ---------------------------------------------------------------------------

def test_credentials_are_redacted_by_key_and_by_shape():
    out = redact({"api_secret": "abc123", "note": "used sk-ant-abcdefghijklmno",
                  "nested": [{"Authorization": "token aaaaaaaa:bbbbbbbb"}]})
    assert out["api_secret"] == MASK
    assert "sk-ant-abcdefghijklmno" not in out["note"]
    assert out["nested"][0]["Authorization"] == MASK


def test_a_trace_survives_a_node_that_raises():
    tr = Tracer(None, run_id="t1")
    with pytest.raises(ValueError), tr.step("boom"):
        raise ValueError("no")
    assert tr.steps[0].outcome == "error" and "no" in tr.steps[0].error


def test_the_excel_report_has_the_three_sheets_and_a_row_per_exception(tmp_path):
    from openpyxl import load_workbook

    class R:
        name = "partial_receipt_qty_short"
        passed = True
        expected_state = actual_state = "completed_with_exceptions"
        exceptions = ["qty_short"]
        rules_cited = ["R4"]
        documents = ["PUR-ORD-2026-00001"]
        steps, llm_calls, ms = 11, 0, 120
        reason = "qty_short x1"
        match = None

    out = tmp_path / "exceptions.xlsx"
    n = write_exception_report([R()], out, summary={"scenarios": 1, "success_rate": 100.0})
    assert n == 1
    wb = load_workbook(out)
    assert wb.sheetnames == ["Summary", "Exceptions", "Run log"]
    assert wb["Exceptions"].freeze_panes == "A2"
    assert wb["Exceptions"].auto_filter.ref


def test_the_report_is_written_even_when_there_are_no_exceptions(tmp_path):
    out = tmp_path / "clean.xlsx"
    assert write_exception_report([], out) == 0
    assert out.exists()


def test_the_fake_survives_a_pickle_round_trip_and_keeps_counting():
    """A run paused at the gate is resumed by a different process.

    `itertools.count` cannot be pickled, so the counters are stored as
    integers and rebuilt. If they restarted at 1 the resumed run would
    create documents whose names collide with the first process's.
    """
    import pickle
    erp = demo_company()
    first = erp.insert("Material Request", {
        "material_request_type": "Purchase", "schedule_date": "2026-12-01",
        "supplier": "Bharat Fasteners Pvt Ltd",
        "items": [{"item_code": "FAS-M8-HEX", "qty": 1, "rate": 12}]})
    revived = pickle.loads(pickle.dumps(erp))
    second = revived.insert("Material Request", {
        "material_request_type": "Purchase", "schedule_date": "2026-12-01",
        "supplier": "Bharat Fasteners Pvt Ltd",
        "items": [{"item_code": "FAS-M8-HEX", "qty": 1, "rate": 12}]})
    assert first.name == "MAT-MR-2026-00001"
    assert second.name == "MAT-MR-2026-00002"
    assert revived.get_doc("Material Request", first.name).name == first.name

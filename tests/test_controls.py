"""Defects found by reading the code after v2, each pinned by a test that failed first.

- Money went through binary floats on the way to integer paise, so `1.005`
  became 100 paise and a delivery exactly on the R4 boundary was an
  exception.
- The resume path had its own copy of the exception handlers, without the
  `VerificationError` branch.
- `run_method` accepted any dotted path, which walks round the doctype
  allowlist through Frappe's generic write methods.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from procureflow.config import RunState
from procureflow.erp.client import ALLOWED_METHODS, ERPError, ERPNextClient, VerificationError
from procureflow.graph import Agent
from procureflow.match import Tolerances, dec, line_paise, paise, three_way_match
from tests.conftest import TODAY, sample

# ---------------------------------------------------------------------------
# Money is decimal all the way to the paisa
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("1.005", 101), ("0.285", 29), (1.005, 101), (0.285, 29),
    ("-1.005", -101), ("2.675", 268), ("1240.005", 124001),
])
def test_half_paisa_rounds_away_from_zero_even_where_a_float_cannot_hold_it(raw, want):
    """`1.005` is `1.00499999...` in binary. The float version returned 100."""
    assert paise(raw) == want


def test_a_float_is_read_as_its_shortest_repr_not_its_binary_expansion():
    assert dec(1.005) == Decimal("1.005")
    assert dec("₹ 1,240.50") == Decimal("1240.50")


@pytest.mark.parametrize("rate_p,qty,want", [
    (12345, 2.5, 30863),          # 308.625 rupees rounds up, not truncated
    (10, 3, 30),                   # 0.10 x 3 is exactly 0.30
    (18200, "2.5", 45500),
    (99, Decimal("0.5"), 50),
])
def test_a_line_value_is_rounded_once_and_never_truncated(rate_p, qty, want):
    assert line_paise(rate_p, qty) == want


def test_a_delivery_exactly_on_the_quantity_boundary_is_inside_tolerance():
    """2% of 60 is 1.2. In floats 61.2 - 60 = 1.2000000000000028 > 1.2."""
    po = type("D", (), {"name": "PO", "items": [{"item_code": "X", "qty": 60, "rate": 100}]})()
    grn = type("D", (), {"name": "GRN", "items": [{"item_code": "X", "qty": 61.2, "rate": 100}]})()
    assert three_way_match(po, grn, None, tol=Tolerances()).matched


def test_one_unit_past_the_boundary_is_still_caught():
    po = type("D", (), {"name": "PO", "items": [{"item_code": "X", "qty": 60, "rate": 100}]})()
    grn = type("D", (), {"name": "GRN", "items": [{"item_code": "X", "qty": 61.3, "rate": 100}]})()
    res = three_way_match(po, grn, None, tol=Tolerances())
    assert [e.type.value for e in res.exceptions] == ["qty_over"]


def test_the_price_band_is_exact_in_paise():
    tol = Tolerances()
    assert tol.pct_limit_paise(124000) == 6200          # 5% of 1,240.00
    assert tol.price_limit(1866000) == 50000             # 5% is 933; the 500 cap binds


# ---------------------------------------------------------------------------
# One set of exception handlers, for the first run and for the resume
# ---------------------------------------------------------------------------


def test_a_verification_failure_after_approval_is_not_reported_as_an_erp_refusal(agent, erp):
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    assert s.state is RunState.AWAITING_APPROVAL

    erp.fail_next(VerificationError("Purchase Order stored a different supplier"),
                  doctype="Purchase Order", op="submit")
    s2, _ = agent.resume(s, tr, approved=True, note="ok", approver="proc.manager")

    assert s2.state is RunState.NEEDS_HUMAN
    assert s2.reason.startswith("read-back verification failed")
    assert "R8" in s2.rules


def test_resume_cites_the_rule_a_refusal_enforces(agent, erp):
    from procureflow.tools import ToolRefused
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    erp.fail_next(ToolRefused("stopped", rule_id="R10"), doctype="Purchase Order", op="submit")
    s2, _ = agent.resume(s, tr, approved=True, approver="proc.manager")
    assert s2.state is RunState.BLOCKED
    assert "R10" in s2.rules


def test_resume_and_run_share_one_walker():
    """Structural: there is exactly one place exceptions become outcomes."""
    import inspect

    from procureflow import graph
    lines = [ln.strip() for ln in inspect.getsource(graph.Agent).splitlines()]
    assert sum(ln.startswith("except VerificationError") for ln in lines) == 1
    assert sum(ln.startswith("except ERPError") for ln in lines) == 1


# ---------------------------------------------------------------------------
# Only the three mapper methods can be called on the server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "frappe.client.set_value", "frappe.client.insert", "frappe.client.delete",
    "evil.module.make_purchase_order",               # suffix match is not a match
])
def test_the_fake_refuses_any_server_method_outside_the_allowlist(erp, path):
    with pytest.raises(ERPError, match="may not be called"):
        erp.run_method(path, source_name="X")


def test_the_rest_client_refuses_before_building_a_request():
    import httpx

    class Boom:
        def handle_request(self, request):                       # noqa: ANN001
            raise AssertionError("a request was sent")

    c = ERPNextClient("http://erp.invalid", "k", "s", max_retries=1)
    c._client = httpx.Client(base_url="http://erp.invalid", transport=Boom())
    with pytest.raises(ERPError, match="may not be called"):
        c.run_method("frappe.client.set_value", doctype="Supplier", name="X",
                     fieldname="bank_account", value="attacker")


def test_the_method_allowlist_is_exactly_the_three_mappers():
    assert len(ALLOWED_METHODS) == 3
    assert all(p.rsplit(".", 1)[-1].startswith("make_purchase_") for p in ALLOWED_METHODS)


def test_the_happy_path_still_routes_through_the_mappers(erp, policy):
    s, _ = Agent(erp, policy=policy).run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.COMPLETED
    assert sum(1 for kind, _, _ in erp.calls if kind == "method") == 3


# ---------------------------------------------------------------------------
# R11: the third leg -- the invoice against the receipt
# ---------------------------------------------------------------------------


def _d(name, rows):
    from procureflow.erp.client import Doc
    return Doc("X", name, {"name": name, "items": [
        {"item_code": c, "qty": q, "rate": r, "uom": "Nos"} for c, q, r in rows]})


def test_billing_more_than_arrived_is_caught_even_when_delivery_matches_the_order():
    res = three_way_match(_d("PO", [("A", 60, 100)]), _d("GRN", [("A", 60, 100)]),
                          _d("INV", [("A", 70, 100)]))
    assert [e.type.value for e in res.exceptions] == ["billed_over_received"]
    assert res.rules_cited == ["R11"]


def test_a_short_delivery_billed_in_full_raises_both_the_delivery_and_the_bill():
    res = three_way_match(_d("PO", [("A", 60, 100)]), _d("GRN", [("A", 45, 100)]),
                          _d("INV", [("A", 60, 100)]))
    assert res.types == {"qty_short", "billed_over_received"}


def test_billing_inside_the_receipt_allowance_is_not_an_exception():
    """59 counted, 60 billed: inside R4's allowance, a counting artefact."""
    res = three_way_match(_d("PO", [("A", 60, 100)]), _d("GRN", [("A", 59, 100)]),
                          _d("INV", [("A", 60, 100)]))
    assert res.matched


def test_billing_less_than_received_is_never_an_exception():
    res = three_way_match(_d("PO", [("A", 60, 100)]), _d("GRN", [("A", 60, 100)]),
                          _d("INV", [("A", 30, 100)]))
    assert res.matched


def test_an_invoice_line_nobody_ordered_is_held():
    res = three_way_match(_d("PO", [("A", 10, 100)]), _d("GRN", [("A", 10, 100)]),
                          _d("INV", [("A", 10, 100), ("FREIGHT", 1, 2500)]))
    assert [(e.type.value, e.item_code) for e in res.exceptions] == [("unordered_line", "FREIGHT")]


def test_with_no_receipt_r9_holds_and_r11_does_not_double_count():
    res = three_way_match(_d("PO", [("A", 10, 100)]), None, _d("INV", [("A", 10, 100)]))
    assert res.types == {"missing_receipt"}


# ---------------------------------------------------------------------------
# Exactly-once writes: a lost response never becomes a second document
# ---------------------------------------------------------------------------


def _count(erp, doctype):
    return len(erp.docs.get(doctype, {}))


def test_a_timeout_after_the_server_committed_does_not_create_a_second_order(agent, erp):
    """The gateway lost the response; the order exists. Adopt it, do not re-send."""
    erp.fail_next(ERPError("504 Gateway Timeout", status=504), doctype="Purchase Order",
                  op="insert", after_commit=True)
    s, tr = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.COMPLETED, s.reason
    assert _count(erp, "Purchase Order") == 1
    po_step = next(x for x in tr.steps if x.node == "create_purchase_order")
    assert "adopted after an ambiguous failure" in po_step.tool_calls[0]["result"]


def test_a_timeout_before_commit_is_retried_and_still_creates_exactly_one(agent, erp):
    erp.fail_next(ERPError("503 Service Unavailable", status=503), doctype="Purchase Order",
                  op="insert")
    s, tr = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.COMPLETED
    assert _count(erp, "Purchase Order") == 1
    po_step = next(x for x in tr.steps if x.node == "create_purchase_order")
    assert "created on attempt 2" in po_step.tool_calls[0]["result"]


def test_a_lost_submit_response_is_settled_by_reading_docstatus(agent, erp):
    erp.fail_next(ERPError("504 Gateway Timeout", status=504), doctype="Purchase Order",
                  op="submit", after_commit=True)
    s, _ = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.COMPLETED, s.reason
    assert s.po.docstatus == 1


def test_a_persistent_outage_stops_with_a_reason_and_no_order(agent, erp):
    erp.fail_next(ERPError("503 Service Unavailable", status=503), doctype="Purchase Order",
                  op="insert", times=5)
    s, _ = agent.run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.NEEDS_HUMAN
    assert _count(erp, "Purchase Order") == 0


def test_a_validation_error_is_never_retried_by_the_reconciler(agent, erp):
    erp.fail_next(ERPError("Schedule Date cannot be before Transaction Date", status=417),
                  doctype="Purchase Order", op="insert", times=5)
    agent.run(sample("req-happy.txt"), today=TODAY)
    assert sum(1 for k, d, _ in erp.calls if k == "insert" and d == "Purchase Order") == 1


def test_the_database_refuses_a_second_document_with_the_same_key(erp):
    from procureflow.erp.client import IDEMPOTENCY_FIELD
    body = {"material_request_type": "Purchase", "schedule_date": "2026-12-01",
            "items": [{"item_code": "FAS-M8-HEX", "qty": 1, "rate": 12}],
            IDEMPOTENCY_FIELD: "pf-x-mr"}
    erp.insert("Material Request", body)
    with pytest.raises(ERPError) as e:
        erp.insert("Material Request", body)
    assert e.value.status == 409


def test_the_rest_client_never_resends_a_create_on_its_own():
    """The transport retries reads. A create gets exactly one POST."""
    import httpx

    class T:
        def __init__(self):
            self.posts = 0

        def handle_request(self, request):                       # noqa: ANN001
            if request.method == "POST":
                self.posts += 1
                return httpx.Response(504, json={}, request=request)
            return httpx.Response(200, json={"data": []}, request=request)

    t = T()
    c = ERPNextClient("http://erp.invalid", "k", "s", max_retries=3)
    c._client = httpx.Client(base_url="http://erp.invalid", transport=t)
    with pytest.raises(ERPError):
        c.insert("Material Request", {"items": []})
    assert t.posts == 1


# ---------------------------------------------------------------------------
# R12: authority, segregation of duties, and the integrity of an approval
# ---------------------------------------------------------------------------

from procureflow import approval as appr  # noqa: E402
from procureflow.policy_rag import read_numbers, verify_numbers  # noqa: E402


def test_the_delegation_of_authority_table_is_read_from_the_policy():
    n = read_numbers()
    assert verify_numbers(n) == []
    assert [(a.approver_id, a.limit_paise) for a in n.approvers] == [
        ("planning.lead", paise(1_00_000)), ("proc.manager", paise(2_00_000)),
        ("finance.head", paise(5_00_000))]


@pytest.mark.parametrize("approver,requester,value,problem", [
    ("", None, 7_440_000, "does not name"),
    ("ceo", None, 7_440_000, "not in the delegation"),
    ("planning.lead", "R. Iyer", 7_440_000, "segregation of duties"),
    ("planning.lead", "r iyer", 7_440_000, "segregation of duties"),
    ("proc.manager", "J. Fernandes", 22_380_000, "exceeds"),
    ("proc.manager", "R. Iyer", 7_440_000, ""),
    ("finance.head", "J. Fernandes", 22_380_000, ""),
])
def test_an_approval_is_checked_for_authority_and_segregation(approver, requester, value, problem):
    _, why = appr.check_approval(approver_id=approver, approvers=read_numbers().approvers,
                                 value_paise=value, requester=requester)
    assert (problem in why) if problem else why == ""


def test_a_refused_approval_leaves_the_order_waiting_not_rejected(agent):
    s, _ = agent.run(sample("req-short-delivery.txt"), approve=True,
                     approver="planning.lead", today=TODAY)
    assert s.state is RunState.AWAITING_APPROVAL
    assert s.po.docstatus == 0
    assert any(c["rule_id"] == "R12" and c["decision"] == "approval refused"
               for c in s.citations)


def test_a_valid_approval_is_recorded_with_the_digest_it_was_given_to(agent):
    s, _ = agent.run(sample("req-over-threshold.txt"), approve=True,
                     approver="proc.manager", today=TODAY)
    assert s.state is RunState.COMPLETED
    accepted = next(c for c in s.citations if c["decision"] == "approval accepted")
    assert s.approval_digest[:12] in accepted["detail"]


def test_an_order_edited_while_awaiting_approval_is_never_submitted(agent, erp):
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    seen = s.approval_digest
    erp.docs["Purchase Order"][s.po.name]["items"][0]["qty"] = 40.0
    s2, _ = agent.resume(s, tr, approved=True, approver="proc.manager")
    assert s2.state is RunState.BLOCKED
    assert "LUB-HYD-68 quantity 4 became 40" in s2.reason
    assert erp.docs["Purchase Order"][s.po.name]["docstatus"] == 0
    assert s2.approval_digest == seen, "a resume must not re-snapshot the order"


def test_a_supplier_swap_after_approval_is_caught_too(agent, erp):
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    erp.docs["Purchase Order"][s.po.name]["supplier"] = "Kaveri Polymers Ltd"
    s2, _ = agent.resume(s, tr, approved=True, approver="proc.manager")
    assert s2.state is RunState.BLOCKED
    assert "supplier" in s2.reason


def test_an_auto_approved_order_is_also_checked_before_submission(erp, policy):
    """No person approved it; the policy did. The guarantee is the same."""

    class EditAfterGate(Agent):
        def n_approval_gate(self, s, tr):
            s = super().n_approval_gate(s, tr)
            erp.docs["Purchase Order"][s.po.name]["items"][0]["rate"] = 99.0
            return s

    s, _ = EditAfterGate(erp, policy=policy).run(sample("req-happy.txt"), today=TODAY)
    assert s.state is RunState.BLOCKED
    assert "rate" in s.reason


def test_the_snapshot_ignores_line_order_but_not_content():
    from procureflow.erp.client import Doc
    a = Doc("PO", "P", {"name": "P", "supplier": "S", "grand_total": 10, "items": [
        {"item_code": "A", "qty": 1, "rate": 5}, {"item_code": "B", "qty": 1.0, "rate": 5}]})
    b = Doc("PO", "P", {"name": "P", "supplier": "S", "grand_total": 10, "items": [
        {"item_code": "B", "qty": 1, "rate": 5.00}, {"item_code": "A", "qty": 1, "rate": 5}]})
    assert appr.digest(appr.snapshot(a)) == appr.digest(appr.snapshot(b))
    b.data["items"][0]["qty"] = 2
    assert appr.digest(appr.snapshot(a)) != appr.digest(appr.snapshot(b))


@pytest.mark.parametrize("name,who", [
    ("req-happy.txt", "R. Iyer"), ("req-messy-typos.txt", "p. deshmukh"),
    ("req-ceiling-breach.txt", None),
])
def test_the_requester_is_read_from_the_sign_off(name, who):
    from procureflow.extract import extract
    assert extract(sample(name)).request.requester == who


# ---------------------------------------------------------------------------
# A paused run on disk is signed; an edited one is refused before unpickling
# ---------------------------------------------------------------------------


def test_a_paused_run_round_trips_through_the_signed_store(tmp_path, monkeypatch):
    from procureflow import state_store
    monkeypatch.delenv("PF_STATE_KEY", raising=False)
    state_store.save("r1", {"order": "PUR-ORD-2026-00001", "value": 7460000}, folder=tmp_path)
    assert state_store.load("r1", folder=tmp_path)["value"] == 7460000


def test_an_edited_paused_run_is_refused_before_it_is_unpickled(tmp_path, monkeypatch):
    from procureflow import state_store
    monkeypatch.delenv("PF_STATE_KEY", raising=False)
    path = state_store.save("r1", {"value": 7460000}, folder=tmp_path)
    raw = bytearray(path.read_bytes())
    raw[-3] ^= 0x01                                   # one bit, anywhere in the body
    path.write_bytes(bytes(raw))
    with pytest.raises(state_store.StateTampered):
        state_store.load("r1", folder=tmp_path)


def test_an_unsigned_pickle_is_refused_outright(tmp_path):
    import pickle

    from procureflow import state_store
    (tmp_path / "r1.pkl").write_bytes(pickle.dumps({"value": 1}))
    with pytest.raises(state_store.StateTampered):
        state_store.load("r1", folder=tmp_path)


def test_the_cli_pauses_refuses_a_bad_approver_and_accepts_a_good_one(tmp_path, monkeypatch):
    """Two processes in spirit: run, then approve -- through the real CLI."""
    from procureflow import cli, state_store
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(state_store, "PAUSED", tmp_path / "paused")
    monkeypatch.setattr(cli.settings, "trace_db", str(tmp_path / "t.sqlite"))
    for fn in ("save", "load", "exists", "discard"):
        orig = getattr(state_store, fn)
        monkeypatch.setattr(state_store, fn,
                            lambda *a, _o=orig, **k: _o(*a, folder=tmp_path / "paused", **k))
    assert cli.main(["run", sample("req-over-threshold.txt")]) == 0
    run_id = next((tmp_path / "paused").glob("*.pkl")).stem
    assert cli.main(["approve", run_id, "--as", "intern"]) == 1
    assert state_store.exists(run_id), "a refused approval must leave the run waiting"
    assert cli.main(["approve", run_id, "--as", "proc.manager", "--note", "ok"]) == 0
    assert not state_store.exists(run_id)

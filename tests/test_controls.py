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
    s2, _ = agent.resume(s, tr, approved=True, note="ok")

    assert s2.state is RunState.NEEDS_HUMAN
    assert s2.reason.startswith("read-back verification failed")
    assert "R8" in s2.rules


def test_resume_cites_the_rule_a_refusal_enforces(agent, erp):
    from procureflow.tools import ToolRefused
    s, tr = agent.run(sample("req-over-threshold.txt"), today=TODAY)
    erp.fail_next(ToolRefused("stopped", rule_id="R10"), doctype="Purchase Order", op="submit")
    s2, _ = agent.resume(s, tr, approved=True)
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

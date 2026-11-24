"""Three-way match: order against receipt against invoice.

The arithmetic half of the system. Nothing in this module calls a model, and
nothing in it is allowed to: a language model is the right tool for reading
"Qty 4 nos @ 1,240.00" off a smudged PDF and the wrong tool for deciding
whether 4 is within tolerance of 5. Extraction is probabilistic and reviewed;
this is deterministic and tested.

Two details that are easy to get wrong and expensive to get wrong:

**Money is integer paise.** Floats cannot represent 0.1, so a tolerance
comparison written in rupees-as-float produces different answers depending
on how the numbers were reached. Every amount here is an `int` of paise and
every comparison is exact. `rupees()` exists only for display.

**R3 and R4 combine their two limits differently, on purpose.** R3 takes the
*tighter* of 5% and ₹500 because money matters at both ends of the
catalogue. R4 takes the *greater* of 2% and 1 unit because quantity variance
at small counts is a packaging artefact, not a commercial event. Inverting
either one produces a system that is technically consistent and practically
useless, so each is tested in both directions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

PAISE = 100
_ONE = Decimal(1)


def dec(value: Any) -> Decimal:
    """A number as the person wrote it, without passing through binary floating point.

    A `float` is converted through its shortest round-tripping repr, so `1.005`
    becomes `Decimal("1.005")` and not `Decimal(1.00499999999999989...)`.
    """
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = re.sub(r"[^\d.\-]", "", str(value).strip().replace(",", ""))
    if text in ("", "-", "."):
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal(0)


def paise(value: Any) -> int:
    """Rupees in any reasonable shape to exact integer paise.

    Accepts what a reader actually returns: `1240`, `1240.00`, `"1,240.00"`,
    `"₹ 1,240.00"`. Rounds half away from zero, which is what an invoice
    does, rather than Python's bankers' rounding.

    The arithmetic is decimal, not float. The float version passed its own
    rounding test (`0.125`, `0.135`) and still turned `1.005` into 100 paise
    and `0.285` into 28, because neither is representable in binary and both
    land a hair *below* the half. A test that only checks the values its
    author thought of is a test of the author.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value * PAISE
    return int((dec(value) * PAISE).quantize(_ONE, rounding=ROUND_HALF_UP))


def line_paise(rate_paise: int, qty: Any) -> int:
    """Exact value of one line: integer paise times a decimal quantity.

    Quantities can be fractional (2.5 kg of granules), so `rate * qty` is the
    one multiplication in the money path that can produce a fraction of a
    paisa. It is rounded once, half away from zero, here -- not truncated by
    an `int()` at whichever call site happened to compute it.
    """
    return int((Decimal(int(rate_paise)) * dec(qty)).quantize(_ONE, rounding=ROUND_HALF_UP))


def rupees(p: int, *, sign: bool = False) -> str:
    """Integer paise to Indian-grouped rupees: 12,34,567.89.

    Indian grouping, not thousands grouping. A finance reviewer reading
    1,234,567 where they expect 12,34,567 stops trusting the whole report.
    """
    neg = p < 0
    p = abs(int(p))
    whole, frac = divmod(p, PAISE)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    out = f"{s}.{frac:02d}"
    if neg:
        return "-" + out
    return ("+" + out) if sign else out


class ExceptionType(str, Enum):
    """The five states a matched line can be in, besides matching."""

    PRICE_VARIANCE = "price_variance"
    QTY_SHORT = "qty_short"
    QTY_OVER = "qty_over"
    DUPLICATE_INVOICE = "duplicate_invoice"
    MISSING_RECEIPT = "missing_receipt"
    BILLED_OVER_RECEIVED = "billed_over_received"
    UNORDERED_LINE = "unordered_line"


@dataclass(frozen=True)
class Tolerances:
    """R3 and R4, as data rather than as constants in a comparison.

    Loaded from the policy so that changing a threshold is a policy edit.
    The defaults here mirror `policy/procurement_policy.md` v1.3 and exist
    so a unit test can construct one without parsing the document.
    """

    price_pct: float = 5.0
    price_abs_paise: int = 500 * PAISE
    qty_pct: float = 2.0
    qty_abs_units: float = 1.0

    def pct_limit_paise(self, po_rate_paise: int) -> int:
        """The percentage half of R3, in exact paise."""
        return int((Decimal(abs(int(po_rate_paise))) * dec(self.price_pct) / 100)
                   .quantize(_ONE, rounding=ROUND_HALF_UP))

    def price_limit(self, po_rate_paise: int) -> int:
        """R3: the tighter of 5% and ₹500."""
        return min(self.pct_limit_paise(po_rate_paise), self.price_abs_paise)

    def qty_limit(self, po_qty: Any) -> Decimal:
        """R4: the greater of 2% and 1 unit.

        Decimal, because the comparison sits exactly on the boundary the
        policy defines: 2% of 60 is 1.2, and in floats `61.2 - 60` is
        `1.2000000000000028`, which made a delivery exactly at the limit an
        over-receipt.
        """
        return max(abs(dec(po_qty)) * dec(self.qty_pct) / 100, dec(self.qty_abs_units))


@dataclass
class MatchException:
    """One line that did not match, with everything needed to act on it."""

    type: ExceptionType
    rule_id: str
    item_code: str
    po_name: str = ""
    receipt_name: str = ""
    invoice_name: str = ""
    expected: str = ""
    actual: str = ""
    variance: str = ""
    variance_pct: float = 0.0
    tolerance: str = ""
    suggested_action: str = ""
    detail: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "Exception": self.type.value,
            "Rule": self.rule_id,
            "Item": self.item_code,
            "Purchase Order": self.po_name,
            "Receipt": self.receipt_name,
            "Invoice": self.invoice_name,
            "Expected": self.expected,
            "Actual": self.actual,
            "Variance": self.variance,
            "Variance %": round(self.variance_pct, 2),
            "Tolerance": self.tolerance,
            "Suggested action": self.suggested_action,
            "Detail": self.detail,
        }


@dataclass
class MatchResult:
    """The outcome of matching one order's worth of documents."""

    po_name: str = ""
    receipt_name: str = ""
    invoice_name: str = ""
    lines_checked: int = 0
    exceptions: list[MatchException] = field(default_factory=list)
    rules_cited: list[str] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return not self.exceptions

    @property
    def types(self) -> set[str]:
        return {e.type.value for e in self.exceptions}

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.exceptions:
            out[e.type.value] = out.get(e.type.value, 0) + 1
        return out


def normalise_invoice_no(raw: str) -> str:
    """R5's comparison key: upper-cased, stripped of everything but alphanumerics.

    `INV-0042`, `inv 0042` and `INV0042` collapse to `INV0042`. An exact
    string check would find only the duplicates that were never a risk.
    """
    return re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()


def find_duplicate_invoice(supplier: str, invoice_no: str,
                           ledger: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """R5. Returns the already-booked invoice, or None.

    `ledger` is any iterable of dicts carrying `supplier` and
    `bill_no`/`supplier_invoice_no`, so this works against the live ERP, the
    fake, or a scenario fixture without caring which.
    """
    key = normalise_invoice_no(invoice_no)
    if not key:
        return None
    for row in ledger:
        if str(row.get("supplier", "")) != str(supplier):
            continue
        existing = row.get("bill_no") or row.get("supplier_invoice_no") or ""
        if normalise_invoice_no(existing) == key:
            return row
    return None


def _by_item(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index lines by item code, summing where an item appears twice.

    A real PO can carry the same item on two lines with different schedule
    dates. Matching line-by-line would then report a phantom shortfall on
    one and an overage on the other, so quantities are aggregated per item
    and the rate is taken as the value-weighted average.
    """
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        code = str(it.get("item_code") or it.get("description") or "").strip()
        if not code:
            continue
        qty = dec(it.get("qty"))
        rate_p = paise(it.get("rate"))
        amount = line_paise(rate_p, qty)
        if code in out:
            prev = out[code]
            prev["qty"] += qty
            prev["amount_paise"] += amount
            if prev["qty"]:
                prev["rate_paise"] = int((Decimal(prev["amount_paise"]) / prev["qty"])
                                         .quantize(_ONE, rounding=ROUND_HALF_UP))
        else:
            out[code] = {"item_code": code, "qty": qty, "rate_paise": rate_p,
                         "amount_paise": amount, "uom": it.get("uom", "Nos")}
    return out


def three_way_match(po: Any, receipt: Any, invoice: Any, *,
                    tol: Tolerances | None = None,
                    duplicate_of: dict[str, Any] | None = None) -> MatchResult:
    """Compare order, receipt and invoice and classify every disagreement.

    `po`, `receipt` and `invoice` are `Doc`-shaped: anything with `.name` and
    `.items`. `receipt` may be None (R9). `duplicate_of`, when given, is the
    already-booked invoice found by `find_duplicate_invoice` (R5).

    Three legs, each checked against the document that is authoritative for it:

    - **Price: invoice against order** (R3). The order is the agreement.
    - **Delivery: receipt against order** (R4). What arrived, against what
      was asked for.
    - **Billing: invoice against receipt** (R11). What we are asked to pay
      for, against what arrived -- plus any invoiced item that was never
      ordered at all.

    v2 ran only the first two, and so was really two two-way matches. A
    supplier who ships 45 of 60 and invoices for 60 tripped R4 (`qty_short`,
    a delivery problem) but nothing said *the invoice itself is wrong*; and
    a line for an item nobody ordered was skipped without a word, because
    the loop walked the order's items rather than the invoice's.
    """
    tol = tol or Tolerances()
    res = MatchResult(
        po_name=getattr(po, "name", "") or "",
        receipt_name=getattr(receipt, "name", "") or "" if receipt else "",
        invoice_name=getattr(invoice, "name", "") or "" if invoice else "",
    )

    if duplicate_of:
        res.exceptions.append(MatchException(
            type=ExceptionType.DUPLICATE_INVOICE, rule_id="R5",
            item_code="—", po_name=res.po_name, invoice_name=res.invoice_name,
            expected="not previously booked",
            actual=f"already booked as {duplicate_of.get('name', 'an earlier invoice')}",
            suggested_action="Block payment. Confirm with the supplier which invoice stands.",
            detail=("Invoice numbers are compared with case and punctuation removed, "
                    "so INV-0042, inv 0042 and INV0042 are one invoice."),
        ))
        res.rules_cited.append("R5")
        return res          # R8: nothing else matters once this fires

    po_items = _by_item(getattr(po, "items", []) or [])
    res.lines_checked = len(po_items)

    if receipt is None:
        res.exceptions.append(MatchException(
            type=ExceptionType.MISSING_RECEIPT, rule_id="R9",
            item_code="—", po_name=res.po_name, invoice_name=res.invoice_name,
            expected="a Purchase Receipt against this order",
            actual="none found",
            suggested_action="Hold the invoice. Chase Stores for the receipt; it usually lands within two working days.",
            detail="Held rather than rejected: the goods are in our building and the paperwork is ours to fix.",
        ))
        res.rules_cited.append("R9")
    else:
        grn_items = _by_item(receipt.items or [])
        for code, po_line in po_items.items():
            grn = grn_items.get(code)
            got = grn["qty"] if grn else Decimal(0)
            want = po_line["qty"]
            delta = got - want
            limit = tol.qty_limit(want)
            if abs(delta) <= limit:
                continue
            kind = ExceptionType.QTY_SHORT if delta < 0 else ExceptionType.QTY_OVER
            pct = float(delta / want * 100) if want else 0.0
            res.exceptions.append(MatchException(
                type=kind, rule_id="R4", item_code=code,
                po_name=res.po_name, receipt_name=res.receipt_name,
                expected=f"{want:g} {po_line['uom']}",
                actual=f"{got:g} {po_line['uom']}",
                variance=f"{delta:+g}", variance_pct=pct,
                tolerance=f"{tol.qty_pct:g}% or {tol.qty_abs_units:g} unit, whichever is greater "
                          f"(= {limit:g})",
                suggested_action=(
                    "Short delivery. Confirm the balance is still coming before paying in full."
                    if kind is ExceptionType.QTY_SHORT else
                    "Over-receipt. Confirm the extra was ordered before it is invoiced."),
                detail="Quantity is checked against the goods receipt, not the order — "
                       "an invoice that agrees with the order proves nothing about what arrived.",
            ))
            res.rules_cited.append("R4")

    if invoice is not None:
        inv_items = _by_item(invoice.items or [])
        for code, po_line in po_items.items():
            inv = inv_items.get(code)
            if not inv:
                continue
            want_p = int(po_line["rate_paise"])
            got_p = int(inv["rate_paise"])
            delta_p = got_p - want_p
            limit_p = tol.price_limit(want_p)
            if delta_p <= limit_p:
                continue            # under-billing is not an exception
            pct = (delta_p / want_p * 100) if want_p else 0.0
            binding = ("the 5% band" if tol.pct_limit_paise(want_p) <= tol.price_abs_paise
                       else f"the {rupees(tol.price_abs_paise)} cash cap")
            res.exceptions.append(MatchException(
                type=ExceptionType.PRICE_VARIANCE, rule_id="R3", item_code=code,
                po_name=res.po_name, invoice_name=res.invoice_name,
                expected=rupees(want_p), actual=rupees(got_p),
                variance=rupees(delta_p, sign=True), variance_pct=pct,
                tolerance=f"{rupees(limit_p)} ({binding} binds)",
                suggested_action="Query the rate with the supplier before payment, or raise a PO amendment.",
                detail="The tighter of 5% and ₹500 applies, so the rule behaves at both "
                       "ends of the catalogue.",
            ))
            res.rules_cited.append("R3")

        # R11, first half: an invoiced item that is not on the order at all.
        # Walking the invoice's items, not the order's, is the whole fix.
        for code, inv in inv_items.items():
            if code in po_items:
                continue
            res.exceptions.append(MatchException(
                type=ExceptionType.UNORDERED_LINE, rule_id="R11", item_code=code,
                po_name=res.po_name, invoice_name=res.invoice_name,
                expected="not on the purchase order", actual=f"{inv['qty']:g} billed at "
                                                             f"{rupees(inv['rate_paise'])}",
                variance=rupees(inv["amount_paise"], sign=True),
                suggested_action="Hold the line. Pay only for what was ordered; "
                                 "a genuine extra needs its own order.",
                detail="An invoice line with no order behind it is how unapproved charges "
                       "(freight, handling, 'misc') reach payment.",
            ))
            res.rules_cited.append("R11")

        # R11, second half: billed more than was received, beyond R4's allowance.
        # Only meaningful when there is a receipt; with none, R9 already holds.
        if receipt is not None:
            for code, inv in inv_items.items():
                if code not in po_items:
                    continue
                grn = grn_items.get(code)
                received = grn["qty"] if grn else Decimal(0)
                over = inv["qty"] - received
                limit = tol.qty_limit(received)
                if over <= limit:
                    continue
                uom = po_items[code]["uom"]
                res.exceptions.append(MatchException(
                    type=ExceptionType.BILLED_OVER_RECEIVED, rule_id="R11", item_code=code,
                    po_name=res.po_name, receipt_name=res.receipt_name,
                    invoice_name=res.invoice_name,
                    expected=f"at most {received:g} {uom} (received)",
                    actual=f"{inv['qty']:g} {uom} billed",
                    variance=f"{over:+g}",
                    variance_pct=float(over / received * 100) if received else 100.0,
                    tolerance=f"R4's allowance on the received quantity (= {limit:g})",
                    suggested_action=(f"Pay for {received:g} at most. Ask the supplier for a "
                                      "credit note or wait for the balance to be received."),
                    detail="The invoice is checked against the goods receipt: paying for goods "
                           "that have not arrived is the overpayment a three-way match exists "
                           "to stop.",
                ))
                res.rules_cited.append("R11")

    res.rules_cited = sorted(set(res.rules_cited))
    return res

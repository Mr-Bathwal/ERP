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
from enum import Enum
from typing import Any

PAISE = 100


def paise(value: Any) -> int:
    """Rupees in any reasonable shape to exact integer paise.

    Accepts what a reader actually returns: `1240`, `1240.00`, `"1,240.00"`,
    `"₹ 1,240.00"`. Rounds half away from zero, which is what an invoice
    does, rather than Python's bankers' rounding.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value * PAISE
    text = str(value).strip()
    text = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    if text in ("", "-", "."):
        return 0
    whole = float(text) * PAISE
    return int(whole + (0.5 if whole >= 0 else -0.5))


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

    def price_limit(self, po_rate_paise: int) -> int:
        """R3: the tighter of 5% and ₹500."""
        pct_limit = int(abs(po_rate_paise) * self.price_pct / 100 + 0.5)
        return min(pct_limit, self.price_abs_paise)

    def qty_limit(self, po_qty: float) -> float:
        """R4: the greater of 2% and 1 unit."""
        return max(abs(po_qty) * self.qty_pct / 100, self.qty_abs_units)


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
        qty = float(it.get("qty") or 0)
        rate_p = paise(it.get("rate"))
        if code in out:
            prev = out[code]
            total_qty = prev["qty"] + qty
            if total_qty:
                prev["rate_paise"] = int(
                    (prev["rate_paise"] * prev["qty"] + rate_p * qty) / total_qty + 0.5)
            prev["qty"] = total_qty
            prev["amount_paise"] += int(rate_p * qty + 0.5)
        else:
            out[code] = {"item_code": code, "qty": qty, "rate_paise": rate_p,
                         "amount_paise": int(rate_p * qty + 0.5),
                         "uom": it.get("uom", "Nos")}
    return out


def three_way_match(po: Any, receipt: Any, invoice: Any, *,
                    tol: Tolerances | None = None,
                    duplicate_of: dict[str, Any] | None = None) -> MatchResult:
    """Compare order, receipt and invoice and classify every disagreement.

    `po`, `receipt` and `invoice` are `Doc`-shaped: anything with `.name` and
    `.items`. `receipt` may be None (R9). `duplicate_of`, when given, is the
    already-booked invoice found by `find_duplicate_invoice` (R5).

    Price is checked **invoice against order** — the order is the agreement.
    Quantity is checked **receipt against order**, never invoice against
    order, because that is the distinction the whole exercise turns on: a
    supplier who ships 45 of 60 and invoices for 60 passes an
    order-to-invoice check perfectly and has overbilled you by a quarter.
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
            got = float(grn["qty"]) if grn else 0.0
            want = float(po_line["qty"])
            delta = got - want
            limit = tol.qty_limit(want)
            if abs(delta) <= limit:
                continue
            kind = ExceptionType.QTY_SHORT if delta < 0 else ExceptionType.QTY_OVER
            pct = (delta / want * 100) if want else 0.0
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
            binding = ("the 5% band" if int(abs(want_p) * tol.price_pct / 100 + 0.5)
                       <= tol.price_abs_paise else f"the {rupees(tol.price_abs_paise)} cash cap")
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

    res.rules_cited = sorted(set(res.rules_cited))
    return res

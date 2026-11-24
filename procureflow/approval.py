"""R12: who may approve, and what exactly an approval is an approval *of*.

The v2 gate answered one question -- "did someone say yes?" -- and that is
three questions short of a control:

**Who said yes?** An approval that does not name its approver cannot be
audited, and one from someone without the authority for the amount is not an
approval. The delegation-of-authority table lives in the policy, next to the
thresholds it qualifies, and is parsed from there like every other number.

**Did they raise it themselves?** Requester and approver being the same
person is the oldest segregation-of-duties failure in purchasing. It is
checked by name against the request's sign-off.

**Yes to what?** An approval is given to a document as it looked at the
time. If the draft Purchase Order is edited afterwards -- in the ERPNext UI,
by another integration, by a person "just fixing the quantity" -- the
approval does not carry over. So the gate records a digest of the order the
approver saw, and the submit step reads the order back from the ERP and
recomputes it immediately before submitting. Any difference voids the
approval. This is the read-back principle applied to a human decision: what
was approved must be what is submitted, and that is checked, not assumed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .match import dec, paise, rupees


@dataclass(frozen=True)
class Approver:
    """One row of the delegation-of-authority table."""

    approver_id: str
    name: str
    role: str
    limit_paise: int


_DOA_ROW = re.compile(
    r"^\|\s*`(?P<id>[a-z][a-z0-9._-]*)`\s*\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<role>[^|]+?)\s*\|"
    r"\s*₹\s*(?P<limit>[\d,]+)\s*\|", re.MULTILINE)


def parse_authority(text: str) -> tuple[Approver, ...]:
    """The delegation-of-authority table out of R12's text."""
    return tuple(
        Approver(m["id"], m["name"].strip(), m["role"].strip(),
                 int(m["limit"].replace(",", "")) * 100)
        for m in _DOA_ROW.finditer(text))


def _person(name: str | None) -> str:
    """'R. Iyer', 'r iyer' and 'R.Iyer' are one person."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def check_approval(*, approver_id: str, approvers: tuple[Approver, ...],
                   value_paise: int, requester: str | None) -> tuple[Approver | None, str]:
    """Return the approver and the reason the approval is not valid ('' if it is)."""
    if not approver_id:
        return None, "the approval does not name its approver"
    ap = next((a for a in approvers if a.approver_id == approver_id), None)
    if ap is None:
        return None, f"{approver_id!r} is not in the delegation-of-authority table"
    if requester and _person(ap.name) == _person(requester):
        return ap, (f"{ap.name} raised this request and may not approve it "
                    "(segregation of duties)")
    if value_paise > ap.limit_paise:
        return ap, (f"{rupees(value_paise)} exceeds the {ap.role}'s authority of "
                    f"{rupees(ap.limit_paise)}")
    return ap, ""


# ---------------------------------------------------------------------------
# What the approver saw
# ---------------------------------------------------------------------------

def _qty(q: Any) -> str:
    return format(dec(q).normalize(), "f")


def snapshot(doc: Any) -> dict[str, Any]:
    """The parts of a Purchase Order an approval is an approval of.

    Supplier, every line's item, quantity, rate and unit, and the grand
    total -- in exact units (decimal quantities, integer paise), with lines
    sorted so that a reordering the server is entitled to make does not
    look like an edit.
    """
    lines = sorted(
        [str(i.get("item_code") or ""), _qty(i.get("qty")), paise(i.get("rate")),
         str(i.get("uom") or "")]
        for i in (doc.items or []))
    return {"name": doc.name, "supplier": str(doc.get("supplier") or ""),
            "lines": lines, "grand_total_paise": paise(doc.get("grand_total"))}


def digest(snap: dict[str, Any]) -> str:
    """SHA-256 of the canonical snapshot."""
    blob = json.dumps(snap, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def describe_change(before: dict[str, Any], after: dict[str, Any]) -> str:
    """What changed between the approved order and the one about to be submitted."""
    out: list[str] = []
    if before.get("supplier") != after.get("supplier"):
        out.append(f"supplier {before.get('supplier')!r} became {after.get('supplier')!r}")
    b = {ln[0]: ln for ln in before.get("lines", [])}
    a = {ln[0]: ln for ln in after.get("lines", [])}
    for code in sorted(set(b) | set(a)):
        if code not in a:
            out.append(f"line {code} was removed")
        elif code not in b:
            out.append(f"line {code} was added ({a[code][1]} at {rupees(a[code][2])})")
        elif b[code] != a[code]:
            if b[code][1] != a[code][1]:
                out.append(f"{code} quantity {b[code][1]} became {a[code][1]}")
            if b[code][2] != a[code][2]:
                out.append(f"{code} rate {rupees(b[code][2])} became {rupees(a[code][2])}")
            if b[code][3] != a[code][3]:
                out.append(f"{code} unit {b[code][3]} became {a[code][3]}")
    if before.get("grand_total_paise") != after.get("grand_total_paise"):
        out.append(f"grand total {rupees(before.get('grand_total_paise', 0))} became "
                   f"{rupees(after.get('grand_total_paise', 0))}")
    return "; ".join(out) or "the stored document differs in a field outside the snapshot"

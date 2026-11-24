"""An in-memory ERP that behaves badly in the same ways the real one does.

A fake that always succeeds is worse than no fake: it lets you write an agent
that only works when nothing goes wrong, and then you discover the gaps in
front of a live instance. So this one reproduces the ERPNext behaviours that
actually break naive integration code:

- **Naming series.** Documents are named `PUR-ORD-2026-00001`, not by anything
  the caller chose. Code that assumes it knows the name it just created fails
  here, which is where you want it to fail.
- **Server-side defaults.** `amount` is recomputed as `qty * rate` on save,
  and `status` is derived. If the caller sends an inconsistent amount, the
  stored document disagrees with the payload -- and `verify=` catches it,
  exactly as it would in production.
- **Mandatory-field validation**, raising the same `ERPError` shape the REST
  client raises after parsing a 417.
- **The draft/submit lifecycle**, including the refusal to submit twice and
  the refusal to receive against a draft order.
- **Fault injection.** `fail_next(...)` makes the next matching call raise, so
  the retry path and the `needs_human` path can be tested deterministically
  rather than by hoping a real server times out during a test run.

It is a test double for the protocol, not a reimplementation of ERPNext. It
knows nothing about accounting, tax or stock ledgers, and it should not.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from .client import (
    ALLOWED_READ,
    ALLOWED_WRITE,
    IDEMPOTENCY_FIELD,
    Doc,
    ERPError,
    VerificationError,
    _compare,
    guard_method,
)

# Minimum fields ERPNext refuses to save without. Kept deliberately short:
# these are the ones a half-built payload actually misses.
MANDATORY: dict[str, tuple[str, ...]] = {
    "Material Request": ("material_request_type", "schedule_date", "items"),
    "Purchase Order": ("supplier", "schedule_date", "items"),
    "Purchase Receipt": ("supplier", "items"),
    "Purchase Invoice": ("supplier", "items"),
}

SERIES = {
    "Material Request": "MAT-MR-{year}-{n:05d}",
    "Purchase Order": "PUR-ORD-{year}-{n:05d}",
    "Purchase Receipt": "MAT-PRE-{year}-{n:05d}",
    "Purchase Invoice": "ACC-PINV-{year}-{n:05d}",
}


@dataclass
class _Fault:
    doctype: str | None
    op: str | None
    error: Exception
    remaining: int
    after_commit: bool = False


@dataclass
class FakeERPClient:
    """Same protocol as `ERPNextClient`, backed by dictionaries."""

    year: int = 2026
    docs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)
    _counters: dict[str, itertools.count] = field(default_factory=dict)
    _faults: list[_Fault] = field(default_factory=list)

    # -- test helpers -----------------------------------------------------

    def seed(self, doctype: str, name: str, **fields: Any) -> Doc:
        """Put a master record in place without going through validation."""
        self.docs.setdefault(doctype, {})[name] = {"name": name, "docstatus": 0, **fields}
        return Doc(doctype, name, self.docs[doctype][name])

    def fail_next(self, error: Exception, *, doctype: str | None = None,
                  op: str | None = None, times: int = 1,
                  after_commit: bool = False) -> None:
        """Arm a fault so the next matching operation raises.

        Without this, testing the retry-and-escalate paths means either
        monkeypatching httpx or accepting that they are untested. They are
        the paths that run on the worst day, so they get real tests.

        `after_commit=True` is the nasty one: the write is stored and *then*
        the call fails, the way a gateway timeout loses the response to a
        request the server finished. A client that retries blind creates a
        second document; one that reconciles by key does not.
        """
        self._faults.append(_Fault(doctype, op, error, times, after_commit))

    def _maybe_fail(self, doctype: str, op: str, *, after_commit: bool = False) -> None:
        for f in self._faults:
            if f.remaining <= 0 or f.after_commit != after_commit:
                continue
            if (f.doctype in (None, doctype)) and (f.op in (None, op)):
                f.remaining -= 1
                raise f.error

    # -- persistence ------------------------------------------------------
    #
    # A run that pauses at the approval gate is resumed by a *different*
    # process, so in fake mode the ERP has to survive being pickled or the
    # draft order simply would not exist on the other side. `itertools.count`
    # cannot be pickled, so the counters are stored as plain integers and
    # rebuilt on load -- which also keeps the naming series moving forward,
    # rather than restarting at 00001 and colliding with the documents the
    # first process already created.

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state["_counters"] = {k: next(v) for k, v in self._counters.items()}
        state["_faults"] = []          # armed faults are per-process
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        counters = state.pop("_counters", {})
        self.__dict__.update(state)
        self._counters = {k: itertools.count(int(v)) for k, v in counters.items()}
        self._faults = []

    def _next_name(self, doctype: str) -> str:
        c = self._counters.setdefault(doctype, itertools.count(1))
        pattern = SERIES.get(doctype, doctype.upper().replace(" ", "-") + "-{year}-{n:05d}")
        return pattern.format(year=self.year, n=next(c))

    @staticmethod
    def _guard(doctype: str, write: bool) -> None:
        pool = ALLOWED_WRITE if write else ALLOWED_READ
        if doctype not in pool:
            verb = "written" if write else "read"
            raise ERPError(f"{doctype!r} may not be {verb} by this application")

    # -- reads ------------------------------------------------------------

    def get_doc(self, doctype: str, name: str) -> Doc:
        self._guard(doctype, write=False)
        self.calls.append(("get", doctype, name))
        self._maybe_fail(doctype, "get")
        row = self.docs.get(doctype, {}).get(name)
        if row is None:
            raise ERPError(f"{doctype} {name} not found", status=404, doctype=doctype)
        return Doc(doctype, name, dict(row))

    def list_docs(self, doctype: str, filters: dict[str, Any] | None = None,
                  fields: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self._guard(doctype, write=False)
        self.calls.append(("list", doctype, str(filters or {})))
        self._maybe_fail(doctype, "list")
        rows = list(self.docs.get(doctype, {}).values())
        for k, v in (filters or {}).items():
            rows = [r for r in rows if str(r.get(k)) == str(v)]
        if fields:
            rows = [{k: r.get(k) for k in fields} for r in rows]
        return rows[:limit]

    def exists(self, doctype: str, name: str) -> bool:
        try:
            self.get_doc(doctype, name)
            return True
        except ERPError as e:
            if e.status == 404:
                return False
            raise

    # -- writes -----------------------------------------------------------

    def _derive(self, doctype: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Everything the server computes rather than accepts.

        This is where a payload and a stored document legitimately diverge,
        and reproducing it is what makes read-back verification meaningful
        in tests rather than tautological.
        """
        row = dict(payload)
        total = 0.0
        items = []
        for raw in row.get("items") or []:
            it = dict(raw)
            qty = float(it.get("qty") or 0)
            rate = float(it.get("rate") or 0)
            it["qty"], it["rate"] = qty, rate
            it["amount"] = round(qty * rate, 2)      # recomputed, never trusted
            it.setdefault("uom", "Nos")
            total += it["amount"]
            items.append(it)
        row["items"] = items
        row["total_qty"] = round(sum(i["qty"] for i in items), 3)
        row["total"] = round(total, 2)
        row.setdefault("currency", "INR")
        row.setdefault("docstatus", 0)
        row["grand_total"] = round(total, 2)
        row["status"] = "Draft" if row["docstatus"] == 0 else "To Receive and Bill"
        return row

    def insert(self, doctype: str, payload: dict[str, Any], *,
               verify: dict[str, Any] | None = None) -> Doc:
        self._guard(doctype, write=True)
        self.calls.append(("insert", doctype, ""))
        self._maybe_fail(doctype, "insert")

        missing = [f for f in MANDATORY.get(doctype, ()) if not payload.get(f)]
        if missing:
            raise ERPError(
                f"{doctype}: mandatory field(s) missing: {', '.join(missing)}",
                status=417, doctype=doctype, payload=payload)

        for ref_field, ref_doctype in (("supplier", "Supplier"),):
            val = payload.get(ref_field)
            if val and not self.exists(ref_doctype, str(val)):
                raise ERPError(f"Could not find {ref_doctype}: {val}",
                               status=417, doctype=doctype, payload=payload)

        for it in payload.get("items") or []:
            code = it.get("item_code")
            if code and not self.exists("Item", str(code)):
                raise ERPError(f"Could not find Item: {code}",
                               status=417, doctype=doctype, payload=payload)

        # The custom field is unique in the database, so a second document
        # with the same key is refused the way MariaDB refuses it: a 409
        # DuplicateEntryError, not a silent second row.
        key = payload.get(IDEMPOTENCY_FIELD)
        if key and any(r.get(IDEMPOTENCY_FIELD) == key
                       for r in self.docs.get(doctype, {}).values()):
            raise ERPError(f"Duplicate entry {key!r} for key '{IDEMPOTENCY_FIELD}'",
                           status=409, doctype=doctype, payload=payload)

        name = self._next_name(doctype)
        row = self._derive(doctype, payload)
        row["name"] = name
        self.docs.setdefault(doctype, {})[name] = row
        self._maybe_fail(doctype, "insert", after_commit=True)

        stored = self.get_doc(doctype, name)
        if verify:
            drift = _compare(verify, stored.data)
            if drift:
                raise VerificationError(
                    f"{doctype} {name} was stored differently from what was sent: "
                    + "; ".join(drift), doctype=doctype, payload=payload)
        return stored

    def submit(self, doctype: str, name: str) -> Doc:
        self._guard(doctype, write=True)
        self.calls.append(("submit", doctype, name))
        self._maybe_fail(doctype, "submit")
        row = self.docs.get(doctype, {}).get(name)
        if row is None:
            raise ERPError(f"{doctype} {name} not found", status=404, doctype=doctype)
        if int(row.get("docstatus", 0)) == 1:
            raise ERPError(f"{doctype} {name} is already submitted",
                           status=417, doctype=doctype)
        row["docstatus"] = 1
        row["status"] = "To Receive and Bill"
        self._maybe_fail(doctype, "submit", after_commit=True)
        stored = self.get_doc(doctype, name)
        if stored.docstatus != 1:                              # pragma: no cover
            raise VerificationError(f"{doctype} {name} did not submit", doctype=doctype)
        return stored

    def run_method(self, dotted_path: str, **kwargs: Any) -> dict[str, Any]:
        """The two ERPNext mapper methods the graph relies on.

        Real ERPNext refuses to map from a draft source document. Reproducing
        that refusal is the reason the approval gate cannot be bypassed by
        calling the mapper early.
        """
        guard_method(dotted_path)
        self.calls.append(("method", dotted_path, str(kwargs)))
        self._maybe_fail(dotted_path, "method")

        if dotted_path.endswith("make_purchase_order"):
            src = self.get_doc("Material Request", kwargs["source_name"])
            if src.docstatus != 1:
                raise ERPError("Material Request must be submitted before mapping",
                               status=417, doctype="Material Request")
            return {
                "doctype": "Purchase Order",
                "supplier": src.get("supplier"),
                "schedule_date": src.get("schedule_date"),
                "material_request": src.name,
                "items": [{"item_code": i["item_code"], "qty": i["qty"],
                           "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                           "schedule_date": src.get("schedule_date"),
                           "material_request": src.name} for i in src.items],
            }

        if dotted_path.endswith("make_purchase_receipt"):
            src = self.get_doc("Purchase Order", kwargs["source_name"])
            if src.docstatus != 1:
                raise ERPError("Purchase Order must be submitted before receiving",
                               status=417, doctype="Purchase Order")
            return {
                "doctype": "Purchase Receipt",
                "supplier": src.get("supplier"),
                "purchase_order": src.name,
                "items": [{"item_code": i["item_code"], "qty": i["qty"],
                           "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                           "purchase_order": src.name} for i in src.items],
            }

        if dotted_path.endswith("make_purchase_invoice"):
            src = self.get_doc("Purchase Order", kwargs["source_name"])
            if src.docstatus != 1:
                raise ERPError("Purchase Order must be submitted before invoicing",
                               status=417, doctype="Purchase Order")
            return {
                "doctype": "Purchase Invoice",
                "supplier": src.get("supplier"),
                "purchase_order": src.name,
                "items": [{"item_code": i["item_code"], "qty": i["qty"],
                           "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                           "purchase_order": src.name} for i in src.items],
            }

        raise ERPError(f"Unknown or non-whitelisted method: {dotted_path}")

    def close(self) -> None:                                   # pragma: no cover
        """Here so the protocol is satisfied; there is nothing to close."""


def demo_company(year: int = 2026) -> FakeERPClient:
    """A fake pre-loaded with the same masters `seed_erpnext.py` creates.

    Keeping the two in step matters: a test that passes against invented
    masters and fails against the seeded ones has told you nothing.
    """
    erp = FakeERPClient(year=year)
    erp.seed("Company", "ProcureFlow Demo Pvt Ltd",
             default_currency="INR", country="India", abbr="PFD")
    erp.seed("Warehouse", "Stores - PFD", company="ProcureFlow Demo Pvt Ltd")

    suppliers = [
        ("Bharat Fasteners Pvt Ltd", True), ("Deccan Steel & Alloys", True),
        ("Kaveri Polymers Ltd", False), ("Nilgiri Abrasives", False),
        ("Sahyadri Lubricants LLP", False), ("Konkan Electricals", False),
    ]
    for nm, preferred in suppliers:
        erp.seed("Supplier", nm, supplier_name=nm, supplier_group="Raw Material",
                 country="India", preferred=int(preferred))

    items = [
        ("FAS-M8-HEX", "Hex bolt M8x40 stainless", "Nos", 12.0),
        ("FAS-M10-HEX", "Hex bolt M10x50 stainless", "Nos", 19.5),
        ("FAS-WSH-M8", "Flat washer M8 zinc", "Nos", 1.8),
        ("STL-SHT-2MM", "Mild steel sheet 2mm 1250x2500", "Nos", 4180.0),
        ("STL-ROD-20", "Bright steel rod 20mm 6m", "Nos", 1240.0),
        ("STL-ANG-50", "MS angle 50x50x5 6m", "Nos", 980.0),
        ("POL-ABS-GR", "ABS granules natural 25kg", "Kg", 182.0),
        ("POL-PVC-SH", "PVC sheet 3mm 1000x2000", "Nos", 740.0),
        ("ABR-FLP-100", "Flap disc 100mm 60 grit", "Nos", 62.0),
        ("ABR-CUT-230", "Cutting wheel 230mm", "Nos", 48.0),
        ("LUB-HYD-68", "Hydraulic oil ISO VG68 210L", "Nos", 18650.0),
        ("LUB-GRS-EP2", "EP2 lithium grease 18kg", "Nos", 4320.0),
        ("ELE-CBL-25", "Copper cable 2.5sqmm 90m", "Nos", 3150.0),
        ("ELE-MCB-32", "MCB 32A C-curve", "Nos", 410.0),
        ("CON-GLV-NIT", "Nitrile gloves pair", "Nos", 28.0),
    ]
    for code, desc, uom, rate in items:
        erp.seed("Item", code, item_name=desc, description=desc, stock_uom=uom,
                 item_group="Raw Material", is_stock_item=1,
                 default_warehouse="Stores - PFD")
        erp.seed("Item Price", f"IP-{code}", item_code=code, price_list="Standard Buying",
                 price_list_rate=rate, currency="INR")
    return erp


def preferred_suppliers(erp: Any) -> list[str]:
    """Supplier names the policy marks preferred (R2)."""
    rows = erp.list_docs("Supplier", fields=["name", "preferred"], limit=100)
    return [r["name"] for r in rows if r.get("preferred")]


_SERIES_RE = re.compile(r"^[A-Z]{3}-[A-Z]{2,4}-\d{4}-\d{5}$")


def looks_like_series(name: str) -> bool:
    """True if `name` matches ERPNext's naming-series shape.

    Used by the tests to assert the agent never assumes a document name it
    chose itself, which is the commonest way an ERP integration breaks the
    first time it meets a real naming series.
    """
    return bool(_SERIES_RE.match(name))

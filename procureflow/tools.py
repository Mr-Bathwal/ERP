"""The only operations the agent may perform.

Every write in this project goes through one of these functions. There is no
generic `call_erp(method, url, body)` and there never should be: the model
reads supplier PDFs that arrive by email, and a document that persuades it
to "update the supplier bank account" must hit a wall built in Python, not a
sentence in a system prompt asking it nicely.

Three guards live here rather than in the graph, because a guard a new node
can forget is not a guard:

- **Allowlist.** Enforced again at this layer even though `erp/client.py`
  enforces it too. Two cheap checks on different sides of a boundary is the
  right amount of paranoia for something that moves money.
- **Dry run.** `ctx.dry_run` short-circuits every write and returns a
  plausible `Doc` so the rest of the graph still exercises its logic. A flag
  checked at each call site would eventually be missed; here it cannot be.
- **R10, the run ceiling.** Checked immediately before the one call that
  creates financial commitment. An approval gate protects against a wrong
  decision; the ceiling protects against a wrong *system*, so it sits below
  the gate and cannot be approved away.

Each function returns `(Doc, ToolCall)`. The `ToolCall` is what `tracing.py`
records — arguments, the documents touched, and the outcome — so the trace
is produced by the same code path that does the work and cannot drift from
it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import RunContext, settings
from .erp.client import ALLOWED_READ, ALLOWED_WRITE, Doc, ERPError
from .match import line_paise, paise, rupees


@dataclass
class ToolCall:
    """One recorded operation, ready for a trace row."""

    name: str
    kind: str = "erp"                      # erp | llm | browser
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    docs: list[str] = field(default_factory=list)
    ms: int = 0
    ok: bool = True
    error: str = ""


class ToolRefused(RuntimeError):
    """A guard stopped this before it reached the ERP.

    Distinct from `ERPError`: the ERP never saw it. These are the cases the
    design intends to refuse, so they are reported as decisions rather than
    as failures. `rule_id` is the policy rule the refusal enforces, so the
    run cites the right one instead of assuming every refusal is R10.
    """

    def __init__(self, message: str, *, rule_id: str = "R10") -> None:
        super().__init__(message)
        self.rule_id = rule_id


def _timed(fn):                                                  # noqa: ANN001
    def wrap(*a: Any, **kw: Any):
        t0 = time.perf_counter()
        call: ToolCall | None = None
        try:
            doc, call = fn(*a, **kw)
            return doc, call
        except (ERPError, ToolRefused) as e:
            call = ToolCall(name=fn.__name__, ok=False, error=str(e)[:400])
            raise
        finally:
            if call is not None:
                call.ms = int((time.perf_counter() - t0) * 1000)
    wrap.__name__ = fn.__name__
    wrap.__doc__ = fn.__doc__
    return wrap


def _fake_doc(doctype: str, payload: dict[str, Any]) -> Doc:
    """What a dry run returns in place of a document it did not create.

    Named `DRYRUN-<doctype>` rather than a realistic series number so that
    nothing downstream can mistake it for a real document, and so a dry-run
    artefact that leaks into a report is obvious on sight.
    """
    data = dict(payload)
    total = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0)
                for i in payload.get("items") or [])
    data.update({"name": f"DRYRUN-{doctype.replace(' ', '-').upper()}",
                 "docstatus": 0, "grand_total": round(total, 2),
                 "total": round(total, 2), "status": "Draft (dry run)"})
    return Doc(doctype, data["name"], data)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def supplier_exists(erp: Any, name: str) -> tuple[bool, ToolCall]:
    """R6's test. Exact match only — no nearest-name guessing, ever."""
    t0 = time.perf_counter()
    found = bool(name) and erp.exists("Supplier", name)
    return found, ToolCall(
        name="supplier_exists", args={"supplier": name},
        result="found" if found else "not found",
        ms=int((time.perf_counter() - t0) * 1000))


def find_supplier_like(erp: Any, name: str, limit: int = 5) -> list[str]:
    """Candidate names for a human to choose from when R6 escalates.

    Offered to the *person*, never acted on by the agent. The distinction
    matters: suggesting "did you mean Deccan Steel & Alloys?" is helpful;
    silently ordering from them because the email said "Decan Steel" is the
    failure R6 exists to prevent.
    """
    if not name:
        return []
    rows = erp.list_docs("Supplier", fields=["name"], limit=200)
    try:
        from rapidfuzz import fuzz, process
        ranked = process.extract(name, [r["name"] for r in rows],
                                 scorer=fuzz.WRatio, limit=limit)
        return [c for c, score, _ in ranked if score >= 60]
    except Exception:                                            # noqa: BLE001
        low = name.lower()[:6]
        return [r["name"] for r in rows if low in r["name"].lower()][:limit]


def booked_invoices(erp: Any, supplier: str) -> list[dict[str, Any]]:
    """Everything already invoiced for this supplier — R5's haystack."""
    return erp.list_docs("Purchase Invoice",
                         filters={"supplier": supplier},
                         fields=["name", "supplier", "bill_no", "grand_total"],
                         limit=200)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

@_timed
def create_material_request(erp: Any, ctx: RunContext, *, supplier: str,
                            items: list[dict[str, Any]], schedule_date: str,
                            warehouse: str | None = None) -> tuple[Doc, ToolCall]:
    """Create and verify a Material Request.

    Verified on `material_request_type` and the line count. Quantities are
    verified per line by the caller rather than here, because ERPNext may
    legitimately re-express a UOM and the comparison needs the conversion
    factor to be meaningful.
    """
    if "Material Request" not in ALLOWED_WRITE:                  # pragma: no cover
        raise ToolRefused("Material Request is not writable by this application")

    payload = {
        "doctype": "Material Request",
        "material_request_type": "Purchase",
        "company": settings.company,
        "transaction_date": ctx.today.isoformat(),
        "schedule_date": schedule_date,
        "supplier": supplier,
        "items": [{"item_code": i["item_code"], "qty": float(i["qty"]),
                   "rate": float(i.get("rate") or 0),
                   "uom": i.get("uom") or "Nos",
                   "schedule_date": schedule_date,
                   "warehouse": warehouse or settings.warehouse} for i in items],
    }
    call = ToolCall(name="create_material_request",
                    args={"supplier": supplier, "lines": len(items),
                          "schedule_date": schedule_date})
    if ctx.dry_run:
        doc = _fake_doc("Material Request", payload)
        call.result = "skipped (dry run)"
        call.docs = [doc.name]
        return doc, call

    doc = erp.insert("Material Request", payload,
                     verify={"material_request_type": "Purchase", "supplier": supplier})
    if len(doc.items) != len(items):
        raise ERPError(f"Material Request {doc.name} stored {len(doc.items)} lines, "
                       f"{len(items)} were sent")
    call.result = f"created {doc.name}"
    call.docs = [doc.name]
    return doc, call


@_timed
def submit_doc(erp: Any, ctx: RunContext, doctype: str, name: str) -> tuple[Doc, ToolCall]:
    """Submit a document and confirm docstatus is 1."""
    call = ToolCall(name="submit_doc", args={"doctype": doctype, "name": name})
    if ctx.dry_run:
        call.result = "skipped (dry run)"
        return Doc(doctype, name, {"name": name, "docstatus": 1}), call
    doc = erp.submit(doctype, name)
    call.result = f"submitted {name}"
    call.docs = [name]
    return doc, call


def make_mapped_doc(erp: Any, candidates: list[str], source_name: str,
                    fallback: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Try ERPNext's own mapper, fall back to building the payload ourselves.

    The dotted paths for these mappers move between ERPNext versions, and a
    hard dependency on one is how an integration breaks silently on upgrade.
    Each candidate is tried in order; if none is whitelisted on the
    installed version, the caller's hand-built payload is used and the run
    records which route it took, so the eval can show it.
    """
    for path in candidates:
        try:
            mapped = erp.run_method(path, source_name=source_name)
            if isinstance(mapped, dict) and mapped.get("items"):
                return mapped, f"mapper:{path.rsplit('.', 1)[-1]}"
        except ERPError:
            continue
    return fallback, "fallback:hand-built"


@_timed
def create_purchase_order(erp: Any, ctx: RunContext, *, mr: Doc, supplier: str,
                          schedule_date: str, ceiling_paise: int) -> tuple[Doc, ToolCall]:
    """Create and verify a Purchase Order from a submitted Material Request.

    **R10 is enforced here**, immediately before the write and after any
    approval. The ceiling is not a policy the approver can waive: it bounds
    what a defect in this system can do, and a human approving twenty things
    that morning is not a defence against a quantity misread by three orders
    of magnitude.
    """
    fallback = {
        "doctype": "Purchase Order", "supplier": supplier,
        "schedule_date": schedule_date, "material_request": mr.name,
        "items": [{"item_code": i["item_code"], "qty": i["qty"],
                   "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                   "schedule_date": schedule_date,
                   "warehouse": settings.warehouse,
                   "material_request": mr.name} for i in mr.items],
    }
    mapped, route = make_mapped_doc(
        erp,
        ["erpnext.stock.doctype.material_request.material_request.make_purchase_order"],
        mr.name, fallback)

    payload = dict(mapped)
    payload["doctype"] = "Purchase Order"
    payload["supplier"] = supplier
    payload["company"] = settings.company
    payload["transaction_date"] = ctx.today.isoformat()
    payload["schedule_date"] = schedule_date
    for it in payload.get("items") or []:
        it.setdefault("warehouse", settings.warehouse)
        it.setdefault("schedule_date", schedule_date)

    total_p = sum(line_paise(paise(i.get("rate")), i.get("qty"))
                  for i in payload.get("items") or [])

    call = ToolCall(name="create_purchase_order",
                    args={"supplier": supplier, "route": route,
                          "value": rupees(total_p)})

    if total_p > ceiling_paise:
        call.ok = False
        call.error = "R10 run ceiling"
        raise ToolRefused(
            f"R10: this run would create a Purchase Order of {rupees(total_p)}, "
            f"above the {rupees(ceiling_paise)} ceiling for automated runs. "
            "Raise it by hand.")

    if ctx.dry_run:
        doc = _fake_doc("Purchase Order", payload)
        call.result = "skipped (dry run)"
        call.docs = [doc.name]
        return doc, call

    doc = erp.insert("Purchase Order", payload, verify={"supplier": supplier})
    call.result = f"created {doc.name} ({route})"
    call.docs = [doc.name]
    return doc, call


@_timed
def create_purchase_receipt(erp: Any, ctx: RunContext, *, po: Doc,
                            received: dict[str, float] | None = None
                            ) -> tuple[Doc, ToolCall]:
    """Record a Purchase Receipt, optionally partial.

    `received` maps item code to the quantity that actually arrived. Partial
    receipt is a first-class case rather than an error path: it is the input
    that makes the three-way match worth running at all.
    """
    fallback = {
        "doctype": "Purchase Receipt", "supplier": po.get("supplier"),
        "purchase_order": po.name,
        "items": [{"item_code": i["item_code"], "qty": i["qty"],
                   "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                   "warehouse": settings.warehouse,
                   "purchase_order": po.name} for i in po.items],
    }
    mapped, route = make_mapped_doc(
        erp,
        ["erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt"],
        po.name, fallback)

    payload = dict(mapped)
    payload["doctype"] = "Purchase Receipt"
    payload["company"] = settings.company
    payload["posting_date"] = ctx.today.isoformat()
    for it in payload.get("items") or []:
        it.setdefault("warehouse", settings.warehouse)
        if received is not None:
            code = it.get("item_code")
            if code in received:
                it["qty"] = float(received[code])
    if received is not None:
        payload["items"] = [i for i in payload.get("items") or []
                            if float(i.get("qty") or 0) > 0]

    call = ToolCall(name="create_purchase_receipt",
                    args={"po": po.name, "route": route,
                          "partial": received is not None})
    if ctx.dry_run:
        doc = _fake_doc("Purchase Receipt", payload)
        call.result = "skipped (dry run)"
        call.docs = [doc.name]
        return doc, call

    doc = erp.insert("Purchase Receipt", payload,
                     verify={"supplier": po.get("supplier")})
    call.result = f"created {doc.name} ({route})"
    call.docs = [doc.name]
    return doc, call


@_timed
def create_purchase_invoice(erp: Any, ctx: RunContext, *, po: Doc,
                            bill_no: str, rates: dict[str, float] | None = None,
                            qty: dict[str, float] | None = None) -> tuple[Doc, ToolCall]:
    """Record a Purchase Invoice, optionally at rates differing from the PO.

    `rates` is how a price-variance scenario is expressed: the supplier
    billed something other than what was agreed. The agent records what the
    supplier sent — it does not correct it — and `match.py` decides whether
    the difference is inside R3.
    """
    fallback = {
        "doctype": "Purchase Invoice", "supplier": po.get("supplier"),
        "purchase_order": po.name,
        "items": [{"item_code": i["item_code"], "qty": i["qty"],
                   "rate": i.get("rate", 0), "uom": i.get("uom", "Nos"),
                   "purchase_order": po.name} for i in po.items],
    }
    mapped, route = make_mapped_doc(
        erp,
        ["erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice"],
        po.name, fallback)

    payload = dict(mapped)
    payload["doctype"] = "Purchase Invoice"
    payload["company"] = settings.company
    payload["posting_date"] = ctx.today.isoformat()
    payload["bill_no"] = bill_no
    for it in payload.get("items") or []:
        code = it.get("item_code")
        if rates and code in rates:
            it["rate"] = float(rates[code])
        if qty and code in qty:
            it["qty"] = float(qty[code])

    call = ToolCall(name="create_purchase_invoice",
                    args={"po": po.name, "bill_no": bill_no, "route": route})
    if ctx.dry_run:
        doc = _fake_doc("Purchase Invoice", payload)
        call.result = "skipped (dry run)"
        call.docs = [doc.name]
        return doc, call

    doc = erp.insert("Purchase Invoice", payload,
                     verify={"supplier": po.get("supplier"), "bill_no": bill_no})
    call.result = f"created {doc.name} ({route})"
    call.docs = [doc.name]
    return doc, call


# ---------------------------------------------------------------------------
# Introspection, for the README and the tests
# ---------------------------------------------------------------------------

TOOLS = (
    "supplier_exists", "find_supplier_like", "booked_invoices",
    "create_material_request", "submit_doc", "create_purchase_order",
    "create_purchase_receipt", "create_purchase_invoice",
)


def describe_permissions() -> dict[str, Any]:
    """What this application may do, as data.

    A test asserts this against the client's allowlists, so the README's
    claim about the permission surface cannot drift from the code.
    """
    return {
        "tools": list(TOOLS),
        "read_doctypes": sorted(ALLOWED_READ),
        "write_doctypes": sorted(ALLOWED_WRITE),
        "run_ceiling": rupees(settings.run_ceiling_paise),
        "raw_http": False,
        "can_create_supplier": False,
    }

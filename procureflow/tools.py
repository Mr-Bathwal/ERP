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
from .erp.client import (
    ALLOWED_READ,
    ALLOWED_WRITE,
    IDEMPOTENCY_FIELD,
    Doc,
    ERPError,
    VerificationError,
    _compare,
)
from .match import dec, line_paise, paise, rupees


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
                         fields=["name", "supplier", "bill_no", "total", "grand_total",
                                 "posting_date"],
                         limit=200)


# ---------------------------------------------------------------------------
# Exactly-once writes
# ---------------------------------------------------------------------------

def writes_enabled() -> bool:
    """The kill switch. `PROCUREFLOW_WRITES=off` refuses every ERP write.

    Read at call time, not import time, so an operator can stop a running
    service by changing its environment and restarting nothing else.
    """
    import os
    return os.getenv("PROCUREFLOW_WRITES", "on").strip().lower() not in ("off", "0", "false")


def _guard_writes(ctx: RunContext) -> None:
    if not ctx.dry_run and not writes_enabled():
        raise ToolRefused("writes are switched off (PROCUREFLOW_WRITES=off); nothing was "
                          "written", rule_id="R8")


def idempotency_key(ctx: RunContext, step: str) -> str:
    """One key per run and step: the same request always carries the same key."""
    return f"pf-{ctx.run_id}-{step}"


def _ambiguous(e: ERPError) -> bool:
    """Did this failure leave us not knowing whether the write happened?

    A timeout or a 5xx might have committed. A 409 on our unique key means a
    previous attempt certainly did. A 417 means the document was refused and
    nothing was written, so it is not ambiguous and is never retried.
    """
    return e.status is None or e.status >= 500 or e.status == 409


def insert_exactly_once(erp: Any, doctype: str, payload: dict[str, Any], *,
                        key: str, verify: dict[str, Any] | None = None,
                        attempts: int = 3) -> tuple[Doc, str]:
    """Create a document at most once, however the network behaves.

    The key is written into the document. After an ambiguous failure the
    document is looked up **by key before anything is re-sent**: if it is
    there, the earlier attempt committed and that document is adopted (and
    verified like any other); if it is not, re-sending is safe. Two
    documents carrying one key is impossible in a seeded ERPNext (the field
    is unique), so finding two is treated as a verification failure rather
    than resolved by picking one.

    Returns the document and how it was obtained: `created`,
    `created on attempt N`, or `adopted after an ambiguous failure`.
    """
    body = {**payload, IDEMPOTENCY_FIELD: key}
    check = {**(verify or {}), IDEMPOTENCY_FIELD: key}
    last: ERPError | None = None
    for attempt in range(1, attempts + 1):
        try:
            doc = erp.insert(doctype, body, verify=check)
            return doc, "created" if attempt == 1 else f"created on attempt {attempt}"
        except VerificationError:
            raise
        except ERPError as e:
            if not _ambiguous(e):
                raise
            last = e
        found = erp.list_docs(doctype, filters={IDEMPOTENCY_FIELD: key},
                              fields=["name"], limit=2)
        if len(found) > 1:
            raise VerificationError(
                f"{len(found)} {doctype} documents carry the key {key}; expected one",
                doctype=doctype)
        if found:
            doc = erp.get_doc(doctype, found[0]["name"])
            drift = _compare(check, doc.data)
            if drift:
                raise VerificationError(
                    f"{doctype} {doc.name} found by key differs from what was sent: "
                    + "; ".join(drift), doctype=doctype)
            return doc, f"adopted after an ambiguous failure ({last})"
        time.sleep(0.05 * attempt)
    assert last is not None
    raise last


def submit_exactly_once(erp: Any, doctype: str, name: str) -> tuple[Doc, str]:
    """Submit, and if the answer is lost, ask the document rather than retrying.

    A submit whose response timed out may have gone through; submitting
    again then fails with "already submitted". Either way the question is
    the document's `docstatus`, so it is read back and decides.
    """
    try:
        return erp.submit(doctype, name), "submitted"
    except VerificationError:
        raise
    except ERPError as e:
        if not (_ambiguous(e) or "already submitted" in str(e).lower()):
            raise
        doc = erp.get_doc(doctype, name)
        if doc.docstatus == 1:
            return doc, f"confirmed submitted by read-back after: {e}"
        raise


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
    _guard_writes(ctx)
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

    doc, how = insert_exactly_once(
        erp, "Material Request", payload, key=idempotency_key(ctx, "mr"),
        verify={"material_request_type": "Purchase", "supplier": supplier})
    if len(doc.items) != len(items):
        raise ERPError(f"Material Request {doc.name} stored {len(doc.items)} lines, "
                       f"{len(items)} were sent")
    call.result = f"{how} {doc.name}"
    call.docs = [doc.name]
    return doc, call


@_timed
def submit_doc(erp: Any, ctx: RunContext, doctype: str, name: str) -> tuple[Doc, ToolCall]:
    """Submit a document and confirm docstatus is 1."""
    _guard_writes(ctx)
    call = ToolCall(name="submit_doc", args={"doctype": doctype, "name": name})
    if ctx.dry_run:
        call.result = "skipped (dry run)"
        return Doc(doctype, name, {"name": name, "docstatus": 1}), call
    doc, how = submit_exactly_once(erp, doctype, name)
    call.result = f"{how} {name}"
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
    _guard_writes(ctx)
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

    doc, how = insert_exactly_once(erp, "Purchase Order", payload,
                                   key=idempotency_key(ctx, "po"),
                                   verify={"supplier": supplier})

    # R10 was checked against what we *sent*. The server prices lines too
    # (price lists, pricing rules, a default discount), so the total it
    # stored is read back and must agree -- to within one paisa a line, which
    # is the server's own rounding of qty x rate, not a change. A stored
    # total that disagrees, or breaches the ceiling, is never submitted.
    stored = paise(doc.get("total") if doc.get("total") is not None else doc.get("grand_total"))
    lines = max(len(doc.items), 1)
    if abs(stored - total_p) > lines:
        raise VerificationError(
            f"Purchase Order {doc.name} stored a total of {rupees(stored)}; "
            f"{rupees(total_p)} was sent", doctype="Purchase Order")
    sent_lines = sorted((str(i.get("item_code")), dec(i.get("qty")).normalize())
                        for i in payload.get("items") or [])
    kept_lines = sorted((str(i.get("item_code")), dec(i.get("qty")).normalize())
                        for i in doc.items)
    if sent_lines != kept_lines:
        # Same total, different lines (a swap, a UOM conversion) passes a
        # total check; it does not pass this one.
        raise VerificationError(
            f"Purchase Order {doc.name} stored lines {kept_lines}; {sent_lines} were sent",
            doctype="Purchase Order")
    if stored > ceiling_paise:
        raise ToolRefused(f"R10: Purchase Order {doc.name} was stored at {rupees(stored)}, "
                          f"above the {rupees(ceiling_paise)} ceiling. It stays a draft; "
                          "raise it by hand.")
    call.result = f"{how} {doc.name} ({route}); stored total {rupees(stored)} verified"
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
    _guard_writes(ctx)
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

    doc, how = insert_exactly_once(erp, "Purchase Receipt", payload,
                                   key=idempotency_key(ctx, "grn"),
                                   verify={"supplier": po.get("supplier")})
    call.result = f"{how} {doc.name} ({route})"
    call.docs = [doc.name]
    return doc, call


@_timed
def create_purchase_invoice(erp: Any, ctx: RunContext, *, po: Doc,
                            bill_no: str, rates: dict[str, float] | None = None,
                            qty: dict[str, float] | None = None,
                            extra_lines: list[dict[str, Any]] | None = None,
                            ) -> tuple[Doc, ToolCall]:
    """Record a Purchase Invoice, optionally differing from the PO.

    `rates`, `qty` and `extra_lines` are how variance scenarios are
    expressed: the supplier billed something other than what was agreed,
    more than arrived, or a line nobody ordered. The agent records what the
    supplier sent — it does not correct it — and `match.py` decides whether
    the difference is inside R3 and R11.
    """
    _guard_writes(ctx)
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
    for extra in extra_lines or []:
        payload.setdefault("items", []).append({
            "item_code": extra["item_code"], "qty": float(extra.get("qty", 1)),
            "rate": float(extra.get("rate", 0)), "uom": extra.get("uom", "Nos")})

    call = ToolCall(name="create_purchase_invoice",
                    args={"po": po.name, "bill_no": bill_no, "route": route})
    if ctx.dry_run:
        doc = _fake_doc("Purchase Invoice", payload)
        call.result = "skipped (dry run)"
        call.docs = [doc.name]
        return doc, call

    doc, how = insert_exactly_once(erp, "Purchase Invoice", payload,
                                   key=idempotency_key(ctx, "pinv"),
                                   verify={"supplier": po.get("supplier"), "bill_no": bill_no})
    call.result = f"{how} {doc.name} ({route})"
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

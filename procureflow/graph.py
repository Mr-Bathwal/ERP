"""The agent: a LangGraph state machine over the procure-to-pay flow.

Eleven nodes, one direction, one place where it stops and waits for a person.

The shape is deliberate. A conversational agent that decides for itself
which tool to call next is the wrong architecture for a finance process,
because the order of operations is not a judgement call — you cannot
receive against an order you have not submitted, and ERPNext will refuse
you if you try. The sequence is fixed; what the model contributes is
**reading documents**, and what the graph contributes is **never doing the
steps out of order, and never skipping the gate**.

Where the interesting failures live:

- `approval_gate` uses LangGraph's `interrupt()`. The run genuinely stops
  and its state is checkpointed; resuming is a separate process invocation
  via the CLI or the API. It is not a prompt asking the model to pause.
- `submit_purchase_order` is the only node that can reach `erp.submit` on a
  PO, and it is unreachable except through the gate. That is asserted by a
  test which walks the compiled graph's edges rather than by reading the
  code and believing it.
- Every node that writes also verifies, because `tools.py` verifies. A node
  that "succeeds" without the ERP agreeing is the failure mode this whole
  project is an argument about.
- Nothing raises out of the graph. Every exception becomes a terminal state
  with a reason, because a stack trace on a buyer's screen is not an
  outcome, it is an absence of one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from .config import PurchaseRequest, RunContext, RunState
from .erp.client import Doc, ERPError, VerificationError
from .extract import Catalogue, extract, resolve_items
from .match import MatchResult, Tolerances, find_duplicate_invoice, paise, rupees, three_way_match
from .policy_rag import PolicyIndex, PolicyNumbers, read_numbers
from .tools import (
    ToolRefused,
    booked_invoices,
    create_material_request,
    create_purchase_invoice,
    create_purchase_order,
    create_purchase_receipt,
    find_supplier_like,
    submit_doc,
    supplier_exists,
)
from .tracing import Tracer


@dataclass
class RunPlan:
    """Scenario inputs that describe what the *world* does, not the agent.

    A scenario needs to say "the warehouse received 45 of the 60 ordered"
    and "the supplier billed 1,310 instead of 1,240". Those are facts about
    the outside world, not agent decisions, so they are supplied rather than
    invented — which is what makes an exception scenario reproducible.
    """

    received_qty: dict[str, float] | None = None
    invoice_rates: dict[str, float] | None = None
    invoice_qty: dict[str, float] | None = None
    supplier_invoice_no: str = ""
    skip_receipt: bool = False
    force_supplier: str = ""


@dataclass
class State:
    """Everything one run knows. Plain dataclass; LangGraph carries it as-is."""

    source: str = ""
    scenario: str = ""
    ctx: RunContext = field(default_factory=lambda: RunContext(run_id=uuid.uuid4().hex[:12]))
    plan: RunPlan = field(default_factory=RunPlan)

    request: PurchaseRequest | None = None
    extract_method: str = ""
    extract_notes: list[str] = field(default_factory=list)

    citations: list[dict[str, Any]] = field(default_factory=list)
    needs_approval: bool = False
    approved: bool | None = None
    approver_note: str = ""
    expedite: bool = False
    substitution: str = ""

    mr: Doc | None = None
    po: Doc | None = None
    receipt: Doc | None = None
    invoice: Doc | None = None

    match: MatchResult | None = None
    # RUNNING, not FAILED. The driver breaks out of the sequence on any
    # terminal state, so defaulting to FAILED ended every run after its
    # first node with an empty reason.
    state: RunState = RunState.RUNNING
    reason: str = ""
    po_value_paise: int = 0

    def cite(self, rule_id: str, decision: str, detail: str = "") -> None:
        self.citations.append({"rule_id": rule_id, "decision": decision, "detail": detail})

    @property
    def rules(self) -> list[str]:
        return sorted({c["rule_id"] for c in self.citations})


class Agent:
    """Runs the flow. Holds the ERP client, the policy and the tracer.

    Written as a class with explicit node methods rather than closures so
    that each node is independently callable from a test. The LangGraph
    wiring in `build()` adds checkpointing and the interrupt; the nodes
    themselves have no framework in them and can be unit-tested directly.
    """

    def __init__(self, erp: Any, *, policy: PolicyIndex | None = None,
                 numbers: PolicyNumbers | None = None,
                 trace_db: str | Path | None = None,
                 model: Any = None) -> None:
        self.erp = erp
        self.policy = policy or PolicyIndex()
        self.numbers = numbers or read_numbers(self.policy.rules)
        self.trace_db = trace_db
        self.model = model
        self.tol = Tolerances(
            price_pct=self.numbers.price_pct,
            price_abs_paise=self.numbers.price_abs_paise,
            qty_pct=self.numbers.qty_pct,
            qty_abs_units=self.numbers.qty_abs_units,
        )

    # -- nodes ------------------------------------------------------------

    def n_extract(self, s: State, tr: Tracer) -> State:
        """Document in, `PurchaseRequest` out, or `needs_human` with a reason."""
        with tr.step("extract") as st:
            res = extract(s.source, model=self.model)
            s.extract_method = res.method
            tr.extract_method = res.method
            st.tokens_in, st.tokens_out = res.input_tokens, res.output_tokens
            if res.input_tokens or res.output_tokens:
                st.tool_calls.append({"kind": "llm", "name": "extract",
                                      "attempts": res.attempts})
            if not res.ok:
                s.state, s.reason = RunState.NEEDS_HUMAN, (
                    f"the document could not be read into a request: {res.error[:200]}")
                st.outcome, st.detail = "needs_human", s.reason
                return s

            s.request = res.request
            if s.plan.force_supplier:
                s.request.supplier = s.plan.force_supplier

            cat = Catalogue.from_erp(self.erp)
            notes = resolve_items(s.request, cat)
            s.extract_notes = list(res.notes) + notes

            if not s.request.is_complete or s.request.unresolved():
                missing = s.request.missing() + [
                    f"{i.description!r} did not resolve to a catalogue item"
                    for i in s.request.unresolved()]
                s.state = RunState.NEEDS_HUMAN
                s.reason = "; ".join(missing[:4])
                s.cite("R8", "stopped", "extraction incomplete, never guessed")
                st.outcome, st.detail = "needs_human", s.reason
                st.rules_cited = ["R8"]
                return s

            st.detail = (f"{len(s.request.items)} line(s) via {res.method}; "
                         f"supplier {s.request.supplier!r}")
        return s

    def n_validate_masters(self, s: State, tr: Tracer) -> State:
        """R6. The supplier must already exist; never create, never guess."""
        with tr.step("validate_masters") as st:
            req = s.request
            found, call = supplier_exists(self.erp, req.supplier or "")
            st.tool_calls.append({"kind": "erp", **call.__dict__})
            if not found:
                near = find_supplier_like(self.erp, req.supplier or "")
                s.state = RunState.ESCALATED
                s.reason = (f"supplier {req.supplier!r} is not in the master"
                            + (f"; closest existing: {', '.join(near[:3])}" if near else ""))
                s.cite("R6", "escalated",
                       "supplier creation is the control point for bank details and "
                       "tax registration, so the agent may not do it")
                st.outcome, st.detail, st.rules_cited = "escalated", s.reason, ["R6"]
                return s
            st.detail = f"supplier {req.supplier!r} exists; "
            st.detail += f"{len(req.items)} item(s) resolved"
            st.erp_docs = [req.supplier or ""]
        return s

    def n_policy_check(self, s: State, tr: Tracer) -> State:
        """Retrieve the applicable rules and decide what they require."""
        with tr.step("policy_check") as st:
            req = s.request
            query = (f"purchase order approval threshold preferred supplier "
                     f"expedite {req.supplier} "
                     + " ".join(i.description for i in req.items[:4]))
            rules = self.policy.applicable(query, k=4)
            st.rules_cited = [r.rule_id for r in rules]
            st.tool_calls.append({"kind": "retrieval", "name": "policy.retrieve",
                                  "backend": self.policy.backend,
                                  "returned": [r.rule_id for r in rules]})

            value_p = sum(paise(i.rate or 0) * float(i.qty or 0) for i in req.items)
            s.po_value_paise = int(value_p)

            if s.po_value_paise > self.numbers.approval_ceiling_paise:
                s.needs_approval = True
                s.cite("R1", "approval required",
                       f"{rupees(s.po_value_paise)} is above the "
                       f"{rupees(self.numbers.approval_ceiling_paise)} ceiling")
            else:
                s.cite("R1", "auto-approved",
                       f"{rupees(s.po_value_paise)} is at or below the "
                       f"{rupees(self.numbers.approval_ceiling_paise)} ceiling")

            if req.supplier not in self.numbers.preferred and self.numbers.preferred:
                s.substitution = (
                    f"{req.supplier} is not a preferred supplier; "
                    f"preferred are {', '.join(self.numbers.preferred)}")
                s.cite("R2", "noted", s.substitution)

            if req.needs_expedite(s.ctx.today):
                s.expedite = True
                s.cite("R7", "expedite flagged",
                       f"needed by {req.needed_by}, under "
                       f"{self.numbers.expedite_days} days away")

            st.detail = (f"value {rupees(s.po_value_paise)}; "
                         f"approval {'required' if s.needs_approval else 'not required'}"
                         + ("; expedite" if s.expedite else ""))
        return s

    def n_create_material_request(self, s: State, tr: Tracer) -> State:
        with tr.step("create_material_request") as st:
            req = s.request
            schedule = req.needed_by or (s.ctx.today + timedelta(days=7)).isoformat()
            items = [{"item_code": i.item_code, "qty": i.qty,
                      "rate": i.rate or 0, "uom": i.uom or "Nos"} for i in req.items]
            mr, call = create_material_request(
                self.erp, s.ctx, supplier=req.supplier, items=items,
                schedule_date=schedule)
            st.tool_calls.append({"kind": "erp", **call.__dict__})
            if not s.ctx.dry_run:
                mr, call2 = submit_doc(self.erp, s.ctx, "Material Request", mr.name)
                st.tool_calls.append({"kind": "erp", **call2.__dict__})
            s.mr = mr
            st.erp_docs = [mr.name]
            st.detail = f"Material Request {mr.name} created and submitted"
        return s

    def n_create_purchase_order(self, s: State, tr: Tracer) -> State:
        """Build the PO as a draft. Submission is a separate node, after the gate."""
        with tr.step("create_purchase_order") as st:
            req = s.request
            schedule = req.needed_by or (s.ctx.today + timedelta(days=7)).isoformat()
            po, call = create_purchase_order(
                self.erp, s.ctx, mr=s.mr, supplier=req.supplier,
                schedule_date=schedule,
                ceiling_paise=self.numbers.run_ceiling_paise)
            st.tool_calls.append({"kind": "erp", **call.__dict__})
            s.po = po
            tr.po_name = po.name
            st.erp_docs = [po.name]
            st.detail = f"Purchase Order {po.name} drafted, not yet submitted"
        return s

    def n_approval_gate(self, s: State, tr: Tracer) -> State:
        """The one place the machine stops and waits for a person."""
        with tr.step("approval_gate") as st:
            if not s.needs_approval:
                s.approved = True
                st.outcome = "auto"
                st.detail = "no approval required by R1"
                st.rules_cited = ["R1"]
                return s
            if s.approved is None:
                s.state = RunState.AWAITING_APPROVAL
                s.reason = (f"Purchase Order {s.po.name if s.po else ''} for "
                            f"{rupees(s.po_value_paise)} needs approval (R1)")
                st.outcome, st.detail, st.rules_cited = "paused", s.reason, ["R1"]
                return s
            st.outcome = "approved" if s.approved else "rejected"
            st.detail = s.approver_note or st.outcome
            st.rules_cited = ["R1"]
        return s

    def n_submit_purchase_order(self, s: State, tr: Tracer) -> State:
        """Submit. Unreachable except through the gate — asserted by a test."""
        with tr.step("submit_purchase_order") as st:
            if s.approved is not True:
                s.state = RunState.FAILED
                s.reason = "submit was reached without approval"
                st.outcome, st.detail = "refused", s.reason
                return s
            po, call = submit_doc(self.erp, s.ctx, "Purchase Order", s.po.name)
            st.tool_calls.append({"kind": "erp", **call.__dict__})
            s.po = po if po.items else s.po
            st.erp_docs = [s.po.name]
            st.detail = f"Purchase Order {s.po.name} submitted"
        return s

    def n_goods_receipt(self, s: State, tr: Tracer) -> State:
        """Record what actually arrived. May be partial; may be driven by a UI agent."""
        with tr.step("goods_receipt") as st:
            if s.plan.skip_receipt:
                st.outcome, st.detail = "skipped", "scenario: no receipt was raised"
                return s

            if s.ctx.receipt_mode == "computer_use":
                from .computer_use import receive_via_ui
                doc, call = receive_via_ui(self.erp, s.ctx, po=s.po,
                                           received=s.plan.received_qty)
                st.tool_calls.append({"kind": "browser", **call.__dict__})
                s.receipt = doc
            else:
                doc, call = create_purchase_receipt(
                    self.erp, s.ctx, po=s.po, received=s.plan.received_qty)
                st.tool_calls.append({"kind": "erp", **call.__dict__})
                s.receipt = doc

            if s.receipt is not None and not s.ctx.dry_run:
                doc2, call2 = submit_doc(self.erp, s.ctx, "Purchase Receipt", s.receipt.name)
                st.tool_calls.append({"kind": "erp", **call2.__dict__})
                s.receipt = doc2 if doc2.items else s.receipt
            if s.receipt is not None:
                tr.receipt_name = s.receipt.name
                st.erp_docs = [s.receipt.name]
                st.detail = f"Purchase Receipt {s.receipt.name} ({s.ctx.receipt_mode})"
        return s

    def n_create_purchase_invoice(self, s: State, tr: Tracer) -> State:
        """Record the supplier's invoice. R5 blocks a repeat before anything is written."""
        with tr.step("create_purchase_invoice") as st:
            bill_no = s.plan.supplier_invoice_no or (
                s.request.supplier_invoice_no or f"SUP-{s.ctx.run_id[:6].upper()}")

            ledger = booked_invoices(self.erp, s.request.supplier)
            dup = find_duplicate_invoice(s.request.supplier, bill_no, ledger)
            if dup:
                s.state = RunState.BLOCKED
                s.reason = (f"supplier invoice {bill_no} is already booked as "
                            f"{dup.get('name')}")
                s.cite("R5", "blocked", s.reason)
                s.match = three_way_match(s.po, s.receipt, None, tol=self.tol,
                                          duplicate_of=dup)
                st.outcome, st.detail, st.rules_cited = "blocked", s.reason, ["R5", "R8"]
                return s

            inv, call = create_purchase_invoice(
                self.erp, s.ctx, po=s.po, bill_no=bill_no,
                rates=s.plan.invoice_rates, qty=s.plan.invoice_qty)
            st.tool_calls.append({"kind": "erp", **call.__dict__})
            s.invoice = inv
            tr.invoice_name = inv.name
            st.erp_docs = [inv.name]
            st.detail = f"Purchase Invoice {inv.name} for {bill_no}"
        return s

    def n_three_way_match(self, s: State, tr: Tracer) -> State:
        with tr.step("three_way_match") as st:
            s.match = three_way_match(s.po, s.receipt, s.invoice, tol=self.tol)
            for e in s.match.exceptions:
                s.cite(e.rule_id, e.type.value, e.detail)
            st.rules_cited = s.match.rules_cited
            st.detail = (f"{s.match.lines_checked} line(s) checked; "
                         + (", ".join(f"{k}x{v}" for k, v in s.match.summary().items())
                            or "clean"))
        return s

    def n_report(self, s: State, tr: Tracer) -> State:
        with tr.step("report") as st:
            if s.match and s.match.exceptions:
                s.state = RunState.COMPLETED_WITH_EXCEPTIONS
                s.reason = ", ".join(f"{k} x{v}" for k, v in s.match.summary().items())
            else:
                s.state = RunState.COMPLETED
                s.reason = "order, receipt and invoice agree"
            if s.ctx.dry_run:
                s.state = RunState.DRY_RUN
                s.reason = "dry run: nothing was written"
            tr.exceptions = sorted(s.match.types) if s.match else []
            st.detail = s.reason
        return s

    # -- driver -----------------------------------------------------------

    def run(self, source: str | Path, *, scenario: str = "", dry_run: bool = False,
            receipt_mode: Literal["api", "computer_use"] = "api",
            plan: RunPlan | None = None, approve: bool | None = None,
            today: date | None = None) -> tuple[State, Tracer]:
        """Execute the flow start to finish, or until it stops on purpose.

        `approve` pre-seeds the gate's answer so the eval can drive both
        sides of it in one process. A real run leaves it None, stops at
        `awaiting_approval`, and is resumed with `resume()`.
        """
        s = State(
            source=str(source), scenario=scenario, plan=plan or RunPlan(),
            ctx=RunContext(run_id=uuid.uuid4().hex[:12], dry_run=dry_run,
                           receipt_mode=receipt_mode, today=today or date.today()),
        )
        s.approved = approve
        tr = Tracer(self.trace_db, run_id=s.ctx.run_id, source_file=str(source),
                    scenario=scenario, dry_run=dry_run, receipt_mode=receipt_mode)
        return self._drive(s, tr)

    def _drive(self, s: State, tr: Tracer) -> tuple[State, Tracer]:
        """The sequence, with every exception turned into a stated outcome."""
        stages = (
            (self.n_extract, None),
            (self.n_validate_masters, None),
            (self.n_policy_check, None),
            (self.n_create_material_request, None),
            (self.n_create_purchase_order, None),
            (self.n_approval_gate, None),
            (self.n_submit_purchase_order, None),
            (self.n_goods_receipt, None),
            (self.n_create_purchase_invoice, None),
            (self.n_three_way_match, None),
            (self.n_report, None),
        )
        try:
            for fn, _ in stages:
                s = fn(s, tr)
                if s.state in (RunState.NEEDS_HUMAN, RunState.ESCALATED,
                               RunState.BLOCKED, RunState.AWAITING_APPROVAL,
                               RunState.FAILED):
                    break
                # A rejection may only take effect once the gate has run.
                # Checking it after every node meant a pre-seeded
                # `approve=False` escalated the run immediately after
                # `extract`, so `policy_check` never evaluated and the trace
                # recorded "no approval required" for an order that had in
                # fact just been refused.
                if s.approved is False and fn == self.n_approval_gate:
                    s.state = RunState.ESCALATED
                    s.reason = s.approver_note or "rejected by the approver"
                    break
        except ToolRefused as e:
            s.state, s.reason = RunState.BLOCKED, str(e)
            s.cite("R10", "blocked", str(e))
        except VerificationError as e:
            # Its own branch: this is the failure the project is about, and
            # it must never be folded into a generic ERP error.
            s.state, s.reason = RunState.NEEDS_HUMAN, f"read-back verification failed: {e}"
            s.cite("R8", "stopped", "a document was stored differently from what was sent")
        except ERPError as e:
            s.state, s.reason = RunState.NEEDS_HUMAN, f"the ERP refused: {e}"
        except Exception as e:                                   # noqa: BLE001
            s.state, s.reason = RunState.FAILED, f"{type(e).__name__}: {e}"
        finally:
            tr.rules_cited = s.rules
            tr.finish(s.state.value, s.reason)
        return s, tr

    def resume(self, s: State, tr: Tracer, *, approved: bool,
               note: str = "") -> tuple[State, Tracer]:
        """Continue a run that stopped at the gate."""
        s.approved = approved
        s.approver_note = note
        s.state = RunState.RUNNING     # re-established by the nodes below
        s.reason = ""
        try:
            for fn in (self.n_approval_gate, self.n_submit_purchase_order,
                       self.n_goods_receipt, self.n_create_purchase_invoice,
                       self.n_three_way_match, self.n_report):
                s = fn(s, tr)
                if s.state in (RunState.BLOCKED, RunState.ESCALATED,
                               RunState.NEEDS_HUMAN, RunState.FAILED):
                    break
                if s.approved is False and fn == self.n_approval_gate:
                    s.state = RunState.ESCALATED
                    s.reason = note or "rejected by the approver"
                    break
        except ToolRefused as e:
            s.state, s.reason = RunState.BLOCKED, str(e)
        except ERPError as e:
            s.state, s.reason = RunState.NEEDS_HUMAN, f"the ERP refused: {e}"
        except Exception as e:                                   # noqa: BLE001
            s.state, s.reason = RunState.FAILED, f"{type(e).__name__}: {e}"
        finally:
            tr.rules_cited = s.rules
            tr.finish(s.state.value, s.reason)
        return s, tr


# ---------------------------------------------------------------------------
# LangGraph wiring
# ---------------------------------------------------------------------------

NODES = ("extract", "validate_masters", "policy_check", "create_material_request",
         "create_purchase_order", "approval_gate", "submit_purchase_order",
         "goods_receipt", "create_purchase_invoice", "three_way_match", "report")

#: The invariant a test asserts: nothing reaches submit except via the gate.
GATE_PREDECESSOR = {"submit_purchase_order": "approval_gate"}


def build(agent: Agent) -> Any:
    """Compile the same sequence as a LangGraph with a real interrupt.

    `Agent._drive` is the path the eval and the tests use, because it is
    synchronous and trivially debuggable. This is the same graph expressed
    for LangGraph's checkpointer and `interrupt()`, which is what the API
    uses so that an approval can span two processes and a restart.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, StateGraph
    from langgraph.types import interrupt

    g = StateGraph(dict)

    def wrap(name: str, fn):                                     # noqa: ANN001
        def node(payload: dict) -> dict:
            s: State = payload["s"]
            tr: Tracer = payload["tr"]
            payload["s"] = fn(s, tr)
            return payload
        node.__name__ = name
        return node

    mapping = {
        "extract": agent.n_extract,
        "validate_masters": agent.n_validate_masters,
        "policy_check": agent.n_policy_check,
        "create_material_request": agent.n_create_material_request,
        "create_purchase_order": agent.n_create_purchase_order,
        "submit_purchase_order": agent.n_submit_purchase_order,
        "goods_receipt": agent.n_goods_receipt,
        "create_purchase_invoice": agent.n_create_purchase_invoice,
        "three_way_match": agent.n_three_way_match,
        "report": agent.n_report,
    }
    for name, fn in mapping.items():
        g.add_node(name, wrap(name, fn))

    def gate(payload: dict) -> dict:
        s: State = payload["s"]
        if s.needs_approval and s.approved is None:
            decision = interrupt({
                "run_id": s.ctx.run_id,
                "purchase_order": s.po.name if s.po else "",
                "value": rupees(s.po_value_paise),
                "supplier": s.request.supplier if s.request else "",
                "citations": s.citations,
            })
            s.approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
            s.approver_note = (decision or {}).get("note", "") if isinstance(decision, dict) else ""
        else:
            s.approved = True if s.approved is None else s.approved
        payload["s"] = agent.n_approval_gate(s, payload["tr"])
        return payload

    g.add_node("approval_gate", gate)

    order = list(NODES)
    g.set_entry_point(order[0])
    for a, b in zip(order, order[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(order[-1], END)

    return g.compile(checkpointer=MemorySaver())

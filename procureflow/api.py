"""HTTP surface: start runs, read traces, approve or reject.

Exists so the approval gate can be exercised the way it would be in
production — by a person on a different machine, minutes later, through a
URL — rather than only by a flag in a test. The dashboard reads from here
when it is run live, and from the exported JSON when it is served statically.

    uvicorn procureflow.api:app --reload --port 8000

Who is calling is decided by a bearer token, never by the request body.
`PF_API_TOKENS` maps tokens to identities (`token=proc.manager,...`); with
no tokens configured every write endpoint refuses (fail closed). Reads --
health, policy, permissions, results -- stay open. The run endpoint takes no
"auto-approve" flag and no approver: approval only happens through
`/approve`, as the token's identity, under R12. Sources must live in the
inbox folder (`PF_INBOX`), so the API cannot be pointed at an arbitrary
file on the server, and CORS allows only the dashboard's origin.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import state_store
from .config import RunState, settings
from .erp.fake import demo_company
from .graph import Agent, RunPlan
from .match import rupees
from .policy_rag import PolicyIndex
from .tools import describe_permissions

app = FastAPI(title="ProcureFlow", docs_url="/docs", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv(
        "PF_CORS_ORIGINS", "http://localhost:8123,http://127.0.0.1:8123").split(",") if o.strip()],
    allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


def _tokens() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in os.getenv("PF_API_TOKENS", "").split(","):
        if "=" in pair:
            tok, who = pair.split("=", 1)
            if tok.strip() and who.strip():
                out[tok.strip()] = who.strip()
    return out


def identity(authorization: str | None) -> str:
    """The caller's identity from `Authorization: Bearer <token>`, or 401."""
    tokens = _tokens()
    if not tokens:
        raise HTTPException(401, "no API tokens are configured (PF_API_TOKENS); writes are off")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "a bearer token is required")
    for known, who in tokens.items():
        if hmac.compare_digest(known.encode(), token.strip().encode()):
            return who
    raise HTTPException(401, "unknown token")


def inbox_path(name: str) -> Path:
    """A source document inside the inbox, or 400. No escaping it, no symlinks."""
    inbox = Path(os.getenv("PF_INBOX", "seed/samples")).resolve()
    candidate = (inbox / name)
    if candidate.is_symlink():
        raise HTTPException(400, "symbolic links are not accepted")
    resolved = candidate.resolve()
    if inbox not in resolved.parents or not resolved.is_file():
        raise HTTPException(400, f"{name!r} is not a document in the inbox")
    return resolved

_POLICY: PolicyIndex | None = None
_RUNS: dict[str, dict[str, Any]] = {}


def policy() -> PolicyIndex:
    global _POLICY
    if _POLICY is None:
        _POLICY = PolicyIndex()
    return _POLICY


def _erp(live: bool):
    if not live:
        return demo_company()
    from .erp.client import ERPNextClient
    if not settings.erp_configured:
        raise HTTPException(503, "ERPNext credentials are not configured")
    return ERPNextClient(settings.erp_url, settings.erp_key, settings.erp_secret)


class RunRequest(BaseModel):
    file: str                      # a name inside the inbox folder, not a path
    live: bool = False
    dry_run: bool = False
    receipt_mode: str = "api"
    supplier_invoice_no: str = ""


class Decision(BaseModel):
    note: str = ""


def _view(s: Any, tr: Any) -> dict[str, Any]:
    return {
        "run_id": tr.run_id,
        "state": s.state.value,
        "reason": s.reason,
        "supplier": s.request.supplier if s.request else None,
        "value": rupees(s.po_value_paise) if s.po_value_paise else None,
        "needs_approval": s.needs_approval,
        "approved": s.approved,
        "approver": s.approver or None,
        "order_digest": s.approval_digest or None,
        "expedite": s.expedite,
        "extract_method": s.extract_method,
        "documents": {
            "material_request": getattr(s.mr, "name", None),
            "purchase_order": getattr(s.po, "name", None),
            "purchase_receipt": getattr(s.receipt, "name", None),
            "purchase_invoice": getattr(s.invoice, "name", None),
        },
        "citations": s.citations,
        "exceptions": [e.as_row() for e in (s.match.exceptions if s.match else [])],
        "steps": [{"seq": x.seq, "node": x.node, "outcome": x.outcome,
                   "ms": x.ms, "detail": x.detail, "rules": x.rules_cited,
                   "docs": x.erp_docs, "error": x.error} for x in tr.steps],
        "total_ms": tr.total_ms,
        "tokens": {"in": tr.tokens_in, "out": tr.tokens_out, "calls": tr.llm_calls},
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "erp_configured": settings.erp_configured,
            "llm_configured": settings.llm_configured,
            "policy_backend": policy().backend,
            "rules": [r.rule_id for r in policy().rules]}


@app.get("/permissions")
def permissions() -> dict[str, Any]:
    """What the agent may and may not do. Published on purpose."""
    return describe_permissions()


@app.get("/policy")
def get_policy() -> dict[str, Any]:
    return {"backend": policy().backend,
            "rules": [r.as_dict() for r in policy().rules]}


@app.post("/runs")
def start_run(req: RunRequest, authorization: str | None = Header(default=None)
              ) -> dict[str, Any]:
    who = identity(authorization)
    source = inbox_path(req.file)
    erp = _erp(req.live)
    agent = Agent(erp, policy=policy(), trace_db=settings.trace_db)
    s, tr = agent.run(source, dry_run=req.dry_run, receipt_mode=req.receipt_mode,
                      plan=RunPlan(supplier_invoice_no=req.supplier_invoice_no),
                      submitted_by=who)
    view = _view(s, tr)
    _RUNS[tr.run_id] = view
    if s.state is RunState.AWAITING_APPROVAL:
        state_store.save(tr.run_id, (s, tr, req.live, None if req.live else erp))
    return view


@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    return list(_RUNS.values())


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    if run_id not in _RUNS:
        raise HTTPException(404, "no such run")
    return _RUNS[run_id]


def _resume(run_id: str, approved: bool, note: str, approver: str) -> dict[str, Any]:
    if not state_store.exists(run_id):
        raise HTTPException(404, "no run is waiting for a decision under that id")
    try:
        s, tr, live, erp_state = state_store.load(run_id)
    except state_store.StateTampered as e:
        raise HTTPException(409, str(e)) from e
    erp = _erp(live) if (live or erp_state is None) else erp_state
    agent = Agent(erp, policy=policy(), trace_db=settings.trace_db)
    s2, tr2 = agent.resume(s, tr, approved=approved, note=note, approver=approver)
    if s2.state is RunState.AWAITING_APPROVAL:
        state_store.save(run_id, (s2, tr2, live, None if live else erp))
    else:
        state_store.discard(run_id)
    view = _view(s2, tr2)
    _RUNS[tr2.run_id] = view
    return view


@app.post("/runs/{run_id}/approve")
def approve(run_id: str, d: Decision,
            authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _resume(run_id, True, d.note, identity(authorization))


@app.post("/runs/{run_id}/reject")
def reject(run_id: str, d: Decision,
           authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _resume(run_id, False, d.note or "rejected", identity(authorization))


@app.get("/results")
def results() -> dict[str, Any]:
    p = Path("eval/results/results.json")
    if not p.exists():
        raise HTTPException(404, "no evaluation results yet")
    import json
    return json.loads(p.read_text(encoding="utf-8"))

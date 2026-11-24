"""Command line: run a request, approve it, evaluate, report.

    procureflow run seed/samples/req-happy.txt
    procureflow run seed/samples/req-over-threshold.txt --dry-run
    procureflow approve <run_id>            # resume a run paused at the gate
    procureflow eval --tag v2
    procureflow report
    procureflow permissions                 # what the agent may and may not do

Runs that pause are held in a small on-disk store so `approve` can be a
separate invocation, which is the point: an approval that only works inside
one Python process is not an approval, it is a prompt.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

from .config import RunState, settings
from .erp.fake import demo_company
from .graph import Agent, RunPlan
from .match import rupees
from .policy_rag import PolicyIndex
from .tools import describe_permissions

PAUSED = Path("eval/results/paused")


def _erp(live: bool):
    if not live:
        return demo_company()
    from .erp.client import ERPNextClient
    if not settings.erp_configured:
        raise SystemExit("ERPNEXT_API_KEY / ERPNEXT_API_SECRET are not set")
    return ERPNextClient(settings.erp_url, settings.erp_key, settings.erp_secret)


def _show(s, tr) -> None:
    print(f"\n  run      {tr.run_id}")
    print(f"  state    {s.state.value}")
    if s.reason:
        print(f"  reason   {s.reason}")
    if s.request:
        print(f"  supplier {s.request.supplier}")
        for i in s.request.items:
            print(f"           {i.qty:g} x {i.item_code or i.description} "
                  f"({i.match_method or 'unresolved'})")
    if s.po_value_paise:
        print(f"  value    {rupees(s.po_value_paise)}")
    for label, doc in (("MR", s.mr), ("PO", s.po), ("GRN", s.receipt), ("INV", s.invoice)):
        if doc is not None:
            print(f"  {label:<8} {doc.name}")
    if s.citations:
        print("  policy")
        for c in s.citations:
            print(f"           {c['rule_id']}: {c['decision']} — {c['detail'][:70]}")
    if s.match and s.match.exceptions:
        print("  exceptions")
        for e in s.match.exceptions:
            print(f"           [{e.rule_id}] {e.type.value}: {e.item_code} "
                  f"{e.expected} -> {e.actual}")
    print("\n  steps")
    for x in tr.steps:
        print(f"    {x.seq:>2}. {x.node:<26} {x.outcome:<10} {x.ms:>5}ms  {x.detail[:58]}")
    print()


def cmd_run(a: argparse.Namespace) -> int:
    agent = Agent(_erp(a.live), policy=PolicyIndex(), trace_db=settings.trace_db)
    plan = RunPlan(supplier_invoice_no=a.bill_no or "")
    s, tr = agent.run(a.file, scenario=a.scenario, dry_run=a.dry_run,
                      receipt_mode=a.mode, plan=plan,
                      approve=True if a.yes else None)
    _show(s, tr)
    if s.state is RunState.AWAITING_APPROVAL:
        PAUSED.mkdir(parents=True, exist_ok=True)
        # Against a real ERPNext the draft order is on the server and
        # resuming only needs the run state. The fake keeps its documents in
        # memory, so without persisting it too the second process would find
        # no purchase order to submit -- which would make the gate look
        # broken in the mode most people try first. The fake is plain data
        # and pickles; a live client holds a socket and does not.
        erp_state = None if a.live else agent.erp
        (PAUSED / f"{tr.run_id}.pkl").write_bytes(
            pickle.dumps((s, tr, a.live, erp_state)))
        print(f"  paused for approval. Resume with:\n"
              f"      procureflow approve {tr.run_id}\n"
              f"      procureflow approve {tr.run_id} --reject\n")
    return 0 if s.state not in (RunState.FAILED,) else 1


def cmd_approve(a: argparse.Namespace) -> int:
    path = PAUSED / f"{a.run_id}.pkl"
    if not path.exists():
        print(f"no paused run {a.run_id}", file=sys.stderr)
        return 2
    s, tr, live, erp_state = pickle.loads(path.read_bytes())
    erp = _erp(live) if (live or erp_state is None) else erp_state
    agent = Agent(erp, policy=PolicyIndex(), trace_db=settings.trace_db)
    s2, tr2 = agent.resume(s, tr, approved=not a.reject, note=a.note or "")
    _show(s2, tr2)
    path.unlink()
    return 0


def cmd_eval(a: argparse.Namespace) -> int:
    from eval.run_eval import main as run_eval_main
    sys.argv = ["run_eval", "--tag", a.tag] + (["--live"] if a.live else [])
    return run_eval_main()


def cmd_report(a: argparse.Namespace) -> int:
    p = Path("eval/results/results.json")
    if not p.exists():
        print("no results yet; run `procureflow eval` first", file=sys.stderr)
        return 2
    s = json.loads(p.read_text(encoding="utf-8"))
    print(f"\n  {s['tag']}  {s['mode']}  ({s['generated_at']})")
    print(f"  success {s['success_rate']}%  ({s['passed']}/{s['scenarios']})")
    print(f"  exceptions detected {s['exception_detection_rate']}%")
    print(f"  p50 {s['p50_ms']}ms   p95 {s['p95_ms']}ms   "
          f"avg steps {s['avg_steps']}   avg LLM calls {s['avg_llm_calls']}\n")
    for r in s["runs"]:
        mark = "ok  " if r["passed"] else "FAIL"
        print(f"    [{mark}] {r['name']:<44} {r['actual_state']}")
    print()
    return 0


def cmd_permissions(_: argparse.Namespace) -> int:
    print(json.dumps(describe_permissions(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="procureflow",
                                 description="An agent that runs procure-to-pay in ERPNext")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one request end to end")
    r.add_argument("file")
    r.add_argument("--live", action="store_true", help="use the real ERPNext")
    r.add_argument("--dry-run", action="store_true", help="write nothing")
    r.add_argument("--yes", action="store_true", help="pre-approve the gate")
    r.add_argument("--mode", choices=["api", "computer_use"], default="api")
    r.add_argument("--bill-no", default="", help="supplier invoice number")
    r.add_argument("--scenario", default="")
    r.set_defaults(fn=cmd_run)

    ap_ = sub.add_parser("approve", help="resume a run paused at the gate")
    ap_.add_argument("run_id")
    ap_.add_argument("--reject", action="store_true")
    ap_.add_argument("--note", default="")
    ap_.set_defaults(fn=cmd_approve)

    e = sub.add_parser("eval", help="run the evaluation harness")
    e.add_argument("--tag", default="v2")
    e.add_argument("--live", action="store_true")
    e.set_defaults(fn=cmd_eval)

    sub.add_parser("report", help="print the last evaluation").set_defaults(fn=cmd_report)
    sub.add_parser("permissions", help="what the agent may do").set_defaults(fn=cmd_permissions)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

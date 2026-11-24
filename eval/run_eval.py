"""Run every scenario and report what actually happened.

The numbers in the README come from here and from nowhere else. If a figure
cannot be produced by this script it does not go in the README, which is the
only discipline that keeps a project like this honest.

What is measured:

- **Task success**: did the run end in the expected state *and* do the
  expected documents, exceptions and cited rules exist? A run that reaches
  `completed` by skipping the three-way match is a failure here, not a pass.
- **Exception-detection accuracy**: of the scenarios that plant a specific
  defect, how many were classified as that defect and nothing else.
- **Cost and latency**: steps, LLM calls, tokens, p50/p95 wall time.
- **Failure breakdown** by category, so the next iteration has somewhere to
  start rather than a single percentage.

Two scenarios deserve explanation because they look like cheating and are
not. `22-erp-validation-error` and `23-verification-failure` inject faults
into the ERP client. They are not testing the happy path with the safety off;
they are the only way to assert that a 417 becomes a stated reason and that
read-back drift is caught, without waiting for a real server to misbehave.
Both are marked `inject_*` in their YAML and both are reported separately.

    python eval/run_eval.py                 # against the fake, no credentials
    python eval/run_eval.py --live          # against a real ERPNext
    python eval/run_eval.py --tag v2        # keep this run alongside v1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from procureflow.config import settings  # noqa: E402
from procureflow.erp.client import ERPError  # noqa: E402
from procureflow.erp.fake import demo_company  # noqa: E402
from procureflow.graph import Agent, RunPlan, State  # noqa: E402
from procureflow.policy_rag import PolicyIndex, read_numbers  # noqa: E402
from procureflow.report_excel import write_exception_report  # noqa: E402
from procureflow.tracing import export_json, percentile  # noqa: E402

SCENARIOS = ROOT / "eval" / "scenarios"
RESULTS = ROOT / "eval" / "results"
TODAY = date(2026, 11, 24)        # fixed, so expedite and date rules are stable


@dataclass
class Outcome:
    """One scenario's result, with enough detail to debug a failure."""

    name: str
    file: str
    passed: bool
    expected_state: str
    actual_state: str
    reason: str = ""
    failures: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    rules_cited: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    steps: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0
    erp_writes: int = 0
    extract_method: str = ""
    category: str = ""

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _load() -> list[dict[str, Any]]:
    out = []
    for p in sorted(SCENARIOS.glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        data["_file"] = p.name
        out.append(data)
    return out


def _writes(erp: Any) -> int:
    return sum(1 for kind, _, _ in getattr(erp, "calls", [])
               if kind in ("insert", "submit"))


def _check(sc: dict[str, Any], s: State, erp: Any, writes: int) -> list[str]:
    """Compare the run against the scenario's stated expectations.

    Every assertion is explicit. A scenario that only checks the final state
    would pass for the wrong reason often enough to be worthless.
    """
    exp = sc.get("expect") or {}
    bad: list[str] = []

    want_state = exp.get("state")
    if want_state and s.state.value != want_state:
        bad.append(f"state: expected {want_state}, got {s.state.value}")

    if (want := exp.get("approval_required")) is not None:
        if bool(s.needs_approval) != bool(want):
            bad.append(f"approval_required: expected {want}, got {s.needs_approval}")

    for rid in exp.get("rules_cited") or []:
        if rid not in s.rules:
            bad.append(f"rule {rid} was not cited (cited: {','.join(s.rules) or 'none'})")

    want_exc = set(exp.get("exceptions") or [])
    got_exc = set(s.match.types) if s.match else set()
    if "exceptions" in exp and want_exc != got_exc:
        bad.append(f"exceptions: expected {sorted(want_exc) or 'none'}, "
                   f"got {sorted(got_exc) or 'none'}")

    if exp.get("no_documents") and (s.mr or s.po):
        bad.append("documents were created when none should have been")

    if exp.get("not_submitted") and s.po is not None and s.po.docstatus == 1:
        bad.append("the purchase order was submitted when it should not have been")

    if (n := exp.get("items_resolved")) is not None:
        got = len([i for i in (s.request.items if s.request else []) if i.item_code])
        if got != n:
            bad.append(f"items_resolved: expected {n}, got {got}")

    if (n := exp.get("erp_writes")) is not None and writes != n:
        bad.append(f"erp_writes: expected {n}, got {writes}")

    if exp.get("expedite") and not s.expedite:
        bad.append("the expedite flag was not raised")

    if exp.get("substitution_noted") and not s.substitution:
        bad.append("no preferred-supplier substitution was noted")

    if (frag := exp.get("reason_contains")):
        if frag.lower() not in (s.reason or "").lower():
            bad.append(f"reason did not mention {frag!r}: {s.reason[:120]!r}")

    for doctype, n in (exp.get("doc_counts") or {}).items():
        got = len(getattr(erp, "docs", {}).get(doctype, {}))
        if got != n:
            bad.append(f"{doctype}: expected exactly {n} document(s), found {got}")

    for doc in exp.get("documents") or []:
        attr = {"material_request": s.mr, "purchase_order": s.po,
                "purchase_receipt": s.receipt, "purchase_invoice": s.invoice}.get(doc)
        if attr is None:
            bad.append(f"{doc} was not created")
    return bad


def _category(sc: dict[str, Any]) -> str:
    """A scenario may name its category; otherwise it is inferred from the name."""
    if sc.get("category"):
        return str(sc["category"])
    n = sc.get("name", "")
    if "duplicate" in n:
        return "duplicate detection"
    if "price" in n:
        return "price variance"
    if "qty" in n or "receipt" in n:
        return "quantity / receipt"
    if "approval" in n or "ceiling" in n:
        return "authority"
    if "supplier" in n:
        return "master data"
    if "pdf" in n or "messy" in n or "quantity" in n or "ambiguous" in n:
        return "extraction"
    if "error" in n or "verification" in n or "transient" in n:
        return "resilience"
    return "flow"


def run_one(sc: dict[str, Any], *, live: bool, policy: PolicyIndex) -> Outcome:
    """Build a world, run the scenario in it, and grade the result."""
    from procureflow.erp.client import ERPNextClient

    if live:
        erp: Any = ERPNextClient(settings.erp_url, settings.erp_key, settings.erp_secret)
    else:
        erp = demo_company()

    # Faults are armed before the run so the retry and escalate paths are
    # exercised deterministically rather than by hoping a server misbehaves.
    if (f := sc.get("inject_fault")) and not live:
        err = ERPError(f.get("message", "injected"), status=f.get("status", 500),
                       doctype=f.get("doctype"))
        erp.fail_next(err, doctype=f.get("doctype"), op=f.get("op"),
                      times=int(f.get("times", 1)),
                      after_commit=bool(f.get("after_commit", False)))

    if sc.get("inject_verification_drift") and not live:
        # Make the stored document disagree with the payload in exactly the
        # way a server-side default or a stray customisation would.
        original = erp.insert

        def drifting(doctype: str, payload: dict, *, verify: dict | None = None):
            if doctype == "Material Request" and payload.get("supplier"):
                payload = {**payload, "supplier": payload["supplier"]}
                doc = original(doctype, payload, verify=None)
                erp.docs[doctype][doc.name]["supplier"] = "Someone Else Entirely"
                if verify:
                    from procureflow.erp.client import VerificationError, _compare
                    drift = _compare(verify, erp.docs[doctype][doc.name])
                    if drift:
                        raise VerificationError(
                            f"{doctype} {doc.name} was stored differently from what "
                            f"was sent: {'; '.join(drift)}", doctype=doctype)
                return doc
            return original(doctype, payload, verify=verify)

        erp.insert = drifting                                    # type: ignore[assignment]

    # Without trace_db the Tracer buffers and never writes, so the
    # harness produced no monitoring evidence at all and the dashboard
    # had nothing to show per scenario.
    agent = Agent(erp, policy=policy, numbers=read_numbers(policy.rules),
                  trace_db=settings.trace_db)
    plan = RunPlan(**(sc.get("plan") or {}))

    # R5 needs a prior invoice to collide with, so the scenario runs twice.
    if sc.get("repeat_with_same_bill_no"):
        first = RunPlan(**{**(sc.get("plan") or {}),
                           "supplier_invoice_no": sc.get("first_bill_no")
                           or plan.supplier_invoice_no})
        agent.run(sc["input"], scenario=sc["name"] + "/first", plan=first,
                  approve=True, approver=sc.get("approver", ""), today=TODAY)

    # History the scenario needs to exist first, e.g. the earlier half of a
    # split order (R15). Each runs to completion in the same ERP.
    for k, prior in enumerate(sc.get("before") or []):
        agent.run(prior["input"], scenario=f"{sc['name']}/before-{k + 1}",
                  plan=RunPlan(**(prior.get("plan") or {})), approve=prior.get("approve"),
                  approver=prior.get("approver", ""), today=TODAY)

    t0 = time.perf_counter()
    s, tr = agent.run(sc["input"], scenario=sc["name"], plan=plan,
                      dry_run=bool(sc.get("dry_run")),
                      approve=sc.get("approve"), approver=sc.get("approver", ""),
                      today=TODAY)

    # Two-process approval, in one process: the run pauses, the world may
    # change while it waits (`tamper`, applied straight to the stored draft
    # order, the way an edit in the ERPNext screen would be), then a decision
    # arrives through `resume()` exactly as the CLI and the API deliver it.
    if (then := sc.get("pause_then")) and s.state.value == "awaiting_approval":
        tamper = then.get("tamper") or {}
        if tamper and not live and s.po is not None:
            row = erp.docs["Purchase Order"][s.po.name]
            if "supplier" in tamper:
                row["supplier"] = tamper["supplier"]
            for it in row.get("items") or []:
                if it.get("item_code") in (tamper.get("qty") or {}):
                    it["qty"] = float(tamper["qty"][it["item_code"]])
                if it.get("item_code") in (tamper.get("rate") or {}):
                    it["rate"] = float(tamper["rate"][it["item_code"]])
        s, tr = agent.resume(s, tr, approved=bool(then.get("approve", True)),
                             note=then.get("note", ""), approver=then.get("approver", ""))
    wall = int((time.perf_counter() - t0) * 1000)

    writes = _writes(erp) if not live else -1
    if sc.get("repeat_with_same_bill_no"):
        writes = -1                   # two runs share the counter; not meaningful

    bad = _check(sc, s, erp, writes)
    docs = [d for d in (getattr(s.mr, "name", None), getattr(s.po, "name", None),
                        getattr(s.receipt, "name", None),
                        getattr(s.invoice, "name", None)) if d]

    return Outcome(
        name=sc["name"], file=sc["_file"], passed=not bad,
        expected_state=(sc.get("expect") or {}).get("state", ""),
        actual_state=s.state.value, reason=s.reason[:200], failures=bad,
        exceptions=sorted(s.match.types) if s.match else [],
        rules_cited=s.rules, documents=docs, steps=len(tr.steps),
        llm_calls=tr.llm_calls, tokens_in=tr.tokens_in, tokens_out=tr.tokens_out,
        ms=wall, erp_writes=writes, extract_method=s.extract_method,
        category=_category(sc),
    )


def summarise(rows: list[Outcome], *, tag: str, live: bool,
              backend: str, elapsed: float) -> dict[str, Any]:
    lat = [r.ms for r in rows]
    exc_rows = [r for r in rows if (r.exceptions or "exception" in r.expected_state)]
    detected = sum(1 for r in exc_rows if r.passed)
    by_cat: dict[str, dict[str, int]] = {}
    for r in rows:
        c = by_cat.setdefault(r.category, {"pass": 0, "fail": 0})
        c["pass" if r.passed else "fail"] += 1

    return {
        "tag": tag,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "live ERPNext" if live else "FakeERPClient",
        "extraction": sorted({r.extract_method for r in rows if r.extract_method}),
        "policy_backend": backend,
        "scenarios": len(rows),
        "passed": sum(1 for r in rows if r.passed),
        "failed": sum(1 for r in rows if not r.passed),
        "success_rate": round(100 * sum(1 for r in rows if r.passed) / max(len(rows), 1), 1),
        "exception_scenarios": len(exc_rows),
        "exception_detection_rate": round(100 * detected / max(len(exc_rows), 1), 1),
        "avg_steps": round(statistics.fmean([r.steps for r in rows]), 1) if rows else 0,
        "avg_llm_calls": round(statistics.fmean([r.llm_calls for r in rows]), 2) if rows else 0,
        "avg_tokens_in": int(statistics.fmean([r.tokens_in for r in rows])) if rows else 0,
        "avg_tokens_out": int(statistics.fmean([r.tokens_out for r in rows])) if rows else 0,
        "p50_ms": int(percentile(lat, 50)),
        "p95_ms": int(percentile(lat, 95)),
        "total_wall_s": round(elapsed, 1),
        "by_category": by_cat,
        "failures": [{"name": r.name, "expected": r.expected_state,
                      "actual": r.actual_state, "why": r.failures}
                     for r in rows if not r.passed],
        "runs": [r.as_dict() for r in rows],
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    s = summary
    L = [f"# Evaluation — {s['tag']}", "",
         f"`{s['generated_at']}` · {s['mode']} · extraction: "
         f"{', '.join(s['extraction']) or 'n/a'} · policy retrieval: {s['policy_backend']}", "",
         "| Metric | Value |", "|---|---|",
         f"| Scenarios | {s['scenarios']} |",
         f"| **Task success rate** | **{s['success_rate']}%** ({s['passed']}/{s['scenarios']}) |",
         f"| Exception-detection rate | {s['exception_detection_rate']}% "
         f"({s['exception_scenarios']} scenarios) |",
         f"| Avg steps per run | {s['avg_steps']} |",
         f"| Avg LLM calls per run | {s['avg_llm_calls']} |",
         f"| Avg tokens in / out | {s['avg_tokens_in']} / {s['avg_tokens_out']} |",
         f"| Latency p50 / p95 | {s['p50_ms']} ms / {s['p95_ms']} ms |",
         f"| Total wall time | {s['total_wall_s']} s |", "",
         "## By category", "", "| Category | Pass | Fail |", "|---|---|---|"]
    for cat, c in sorted(s["by_category"].items()):
        L.append(f"| {cat} | {c['pass']} | {c['fail']} |")
    L += ["", "## Every scenario", "",
          "| Scenario | Expected | Actual | Rules | Exceptions | ms |",
          "|---|---|---|---|---|---|"]
    for r in s["runs"]:
        tick = "ok" if r["passed"] else "**FAIL**"
        L.append(f"| {r['name']} | {r['expected_state']} | {r['actual_state']} {tick} | "
                 f"{','.join(r['rules_cited']) or '—'} | "
                 f"{','.join(r['exceptions']) or '—'} | {r['ms']} |")
    if s["failures"]:
        L += ["", "## Failures", ""]
        for f in s["failures"]:
            L.append(f"- **{f['name']}** — expected `{f['expected']}`, got `{f['actual']}`")
            for w in f["why"]:
                L.append(f"  - {w}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the ProcureFlow evaluation")
    ap.add_argument("--live", action="store_true", help="run against a real ERPNext")
    ap.add_argument("--tag", default="v1", help="label this run, e.g. v1 / v2")
    ap.add_argument("--only", default="", help="substring filter on scenario name")
    args = ap.parse_args()

    if args.live and not settings.erp_configured:
        print("ERPNEXT_API_KEY / ERPNEXT_API_SECRET are not set", file=sys.stderr)
        return 2

    RESULTS.mkdir(parents=True, exist_ok=True)
    policy = PolicyIndex()
    scenarios = [s for s in _load() if args.only in s.get("name", "")]
    if not scenarios:
        print("no scenarios matched", file=sys.stderr)
        return 2

    print(f"running {len(scenarios)} scenarios against "
          f"{'live ERPNext' if args.live else 'FakeERPClient'}"
          f" (policy retrieval: {policy.backend})\n")

    t0 = time.perf_counter()
    rows: list[Outcome] = []
    for sc in scenarios:
        try:
            r = run_one(sc, live=args.live, policy=policy)
        except Exception as e:                                   # noqa: BLE001
            r = Outcome(name=sc.get("name", sc["_file"]), file=sc["_file"], passed=False,
                        expected_state=(sc.get("expect") or {}).get("state", ""),
                        actual_state="harness_error",
                        failures=[f"{type(e).__name__}: {e}"],
                        category=_category(sc))
        rows.append(r)
        mark = " ok " if r.passed else "FAIL"
        print(f"  [{mark}] {r.name:42} {r.actual_state:26} {r.ms:>5}ms")
        for w in r.failures:
            print(f"         {w}")

    elapsed = time.perf_counter() - t0
    summary = summarise(rows, tag=args.tag, live=args.live,
                        backend=policy.backend, elapsed=elapsed)

    (RESULTS / f"results-{args.tag}.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (RESULTS / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_markdown(summary, RESULTS / f"results-{args.tag}.md")
    write_markdown(summary, RESULTS / "results.md")

    n = write_exception_report(rows, RESULTS / "exceptions.xlsx", summary=summary)
    export_json(settings.trace_db, RESULTS / "traces.json")

    print(f"\n  success {summary['success_rate']}%  "
          f"({summary['passed']}/{summary['scenarios']})   "
          f"p50 {summary['p50_ms']}ms  p95 {summary['p95_ms']}ms   "
          f"{elapsed:.1f}s total")
    print(f"  wrote results-{args.tag}.json/.md, exceptions.xlsx ({n} rows), traces.json")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

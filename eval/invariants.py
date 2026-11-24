"""Properties that must hold on every trace, whatever the scenario expected.

A scenario checks that one run ended where its author said it would. These
check what no run may ever do, so they catch the failure a scenario author
did not think of:

- **The gate.** A Purchase Order is submitted only after an approval gate
  step that approved it (or found no approval was needed).
- **Stops stop.** After a step ends a run -- blocked, paused, refused,
  escalated, needs_human -- no ERP write follows, until a gate step
  approves the order on resume.
- **Dry runs write nothing.**
- **Every rule cited exists** in the policy the run was decided under.
"""

from __future__ import annotations

from typing import Any

STOPS = {"blocked", "paused", "refused", "escalated", "needs_human", "error"}
WRITES = {"create_material_request", "create_purchase_order", "create_purchase_receipt",
          "create_purchase_invoice", "submit_doc"}


def _writes(step: Any) -> list[dict[str, Any]]:
    return [c for c in step.tool_calls
            if c.get("kind") == "erp" and c.get("name") in WRITES and c.get("ok", True)
            and "dry run" not in str(c.get("result", ""))]


def check(tr: Any, *, rule_ids: set[str], dry_run: bool = False) -> list[str]:
    """Violations found in one run's trace. Empty means every invariant held."""
    bad: list[str] = []
    gate_ok = False
    stopped_at = ""
    for st in tr.steps:
        if st.node == "approval_gate":
            if st.outcome in ("approved", "auto"):
                gate_ok, stopped_at = True, ""
        if st.node == "submit_purchase_order" and st.outcome == "ok" and not gate_ok:
            bad.append(f"step {st.seq}: the order was submitted without an approved gate")
        writes = _writes(st)
        if writes and stopped_at:
            bad.append(f"step {st.seq} ({st.node}) wrote after the run stopped at {stopped_at}")
        if writes and dry_run:
            bad.append(f"step {st.seq} ({st.node}) wrote during a dry run")
        for rid in st.rules_cited:
            if rid not in rule_ids:
                bad.append(f"step {st.seq} cites {rid}, which is not in the policy")
        if st.outcome in STOPS:
            stopped_at = stopped_at or f"step {st.seq} ({st.node}: {st.outcome})"
    return bad

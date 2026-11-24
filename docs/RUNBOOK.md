# Runbook — cutover, monitoring, and what breaks

For whoever is on the end of the phone after go-live.

---

## 1. Cutover checklist

Work top to bottom. Do not start a section until the previous one is signed
off.

### Before

- [ ] ERPNext reachable; version recorded
- [ ] Company, fiscal year and currency match [CONFIGURATION.md](CONFIGURATION.md)
- [ ] `python seed/seed_erpnext.py --dry-run` reports only the `+` lines you expect
- [ ] `python seed/seed_erpnext.py`, then **run it again** — the second run must be all `=`
- [ ] Masters spot-checked in the UI: 6 suppliers, 15 items, 1 warehouse, 15 prices
- [ ] API key issued to a **dedicated integration user**, not a person's login
- [ ] `.env` populated; `git status` clean — `.env` must not appear

### Dry run

- [ ] `make test` — 80 passing
- [ ] `make eval` — record the success rate as the pre-go-live baseline
- [ ] `python -m procureflow.cli run <file> --dry-run` ends `dry_run` with zero writes
- [ ] `python -m procureflow.cli permissions` shows `can_create_supplier: false`

### Approval routing

- [ ] An over-threshold request pauses and the PO stays at `docstatus: 0`
- [ ] `procureflow approve <id>` submits it
- [ ] `procureflow approve <id> --reject` escalates and leaves the PO draft
- [ ] The named approver can actually be reached out of hours

### Live

- [ ] One real low-value request, watched end to end
- [ ] All four documents verified **in the ERPNext UI**, not only in the trace
- [ ] `exceptions.xlsx` opened by the person who will own it
- [ ] Rollback understood: cancel in reverse order (Invoice, Receipt, PO, MR).
      An agent run creates nothing a buyer cannot cancel by hand.

### After

- [ ] Day 1: read every trace row
- [ ] Week 1: review the `needs_human` reasons — they are the backlog
- [ ] Week 2: revisit thresholds with the controller using real variance data
- [ ] Rotate the API key when the implementation team hands over

---

## 2. Monitoring

Traces are in SQLite at `PF_TRACE_DB`: one row per run, one per node.

```sql
-- Runs that stopped for a person, and why. The daily list.
SELECT state, reason, COUNT(*) FROM runs
WHERE state IN ('needs_human','escalated','blocked')
GROUP BY state, reason ORDER BY 3 DESC;
```

```sql
-- Where the time goes.
SELECT node, COUNT(*), AVG(ms), MAX(ms) FROM steps
GROUP BY node ORDER BY 3 DESC;
```

```sql
-- Extraction quality. Retries are the leading indicator of drift.
SELECT extract_method, COUNT(*) FROM runs GROUP BY extract_method;
```

**Watch these four:**

| Signal | Healthy | Act when |
|---|---|---|
| `needs_human` rate | under 15% | above 25% for two days: extraction or masters have drifted |
| `extract_method = llm-retry` | under 10% | rising: the model or the document format changed |
| verification failures | **zero** | **ever.** One is an incident |
| p95 latency | under 3 s | above 10 s: ERPNext is struggling, not the agent |

A `needs_human` is **not an error**. It is the designed outcome when policy
asks for a person. Only `failed` means the system did something it did not
intend, and that number should be zero.

---

## 3. The six failure modes

### 1. `escalated: supplier 'X' is not in the master` — R6

**Cause:** a new vendor, or a spelling the master does not carry.
**Fix:** onboard the supplier through the normal process, then re-run. The
run prints the closest existing names; those are for the human to read, and
the agent will not act on them.
**Do not** widen the write allowlist so the agent can create suppliers.

### 2. `needs_human: 'X' did not resolve to a catalogue item`

**Cause:** either the item is genuinely absent, or two codes matched equally
well and the margin rule refused to guess between them.
**Fix:** add the item, or add the requester's wording to the item
`description` so the next match is unambiguous.
**Diagnose:** the CLI prints the best guess and the runner-up score.

### 3. `needs_human: the ERP refused: <message>`

**Cause:** ERPNext validation — most often a schedule date before the
transaction date, or a closed fiscal period.
**Fix:** the message is the server's own, unwrapped from its HTML. Act on it
directly. If it mentions a period, accounts must open it.

### 4. `blocked: R10 ... above the ceiling for automated runs`

**Cause:** either a genuinely large order, or a misread quantity.
**Fix: check the quantity first.** R10 exists precisely because a quantity
misread by three orders of magnitude would otherwise reach an approver who
is approving twenty things that morning. If the value is real, raise the
order by hand.

### 5. `needs_human: read-back verification failed`

**Cause:** the stored document differs from what was sent. A customisation,
a server script or a workflow is rewriting the field.
**Fix:** this is an incident. Stop automated runs, find what is rewriting
the field, and do not resume until it is understood.
**Do not** relax the `verify=` set to make it pass.

### 6. A run hangs or times out

**Cause:** ERPNext background workers are down; the API accepts and never
completes.
**Fix:** check the queue container logs. The client already retries timeouts
and 5xx three times with backoff, so a hang that survives that is the
server, not the agent.

---

## 4. Support playbook

**Triage, in order:**

1. `python -m procureflow.cli report` — is this one run or all of them?
2. `GET /health` — are the ERP and the LLM configured and reachable?
3. The trace for the run — which node, which outcome, which rule.
4. The ERPNext UI — does the document exist, and at what docstatus?

**Escalate immediately:** any verification failure, any `failed` state, any
duplicate that was *not* caught.

**Safe to retry:** `needs_human`, once the underlying master data is fixed.
Re-running creates a new run rather than reusing the old documents, and R5
will block a second invoice against the same supplier invoice number.

**Never** edit `policy/procurement_policy.md` to make a failing run pass
without the controller's sign-off. The document is the control, and a
threshold changed to clear a backlog is an audit finding.

# Runbook — operating ProcureFlow v3

For whoever is on the end of the phone after go-live.

`procureflow` below is the console script from `pyproject.toml`, available
after `pip install -e .`. Without it, `python -m procureflow.cli` does the
same thing.

Two facts shape most of this page:

- **A stop is an outcome, not an error.** `needs_human`, `escalated`,
  `blocked` and `awaiting_approval` are the designed result when the policy
  asks for a person. Only `failed` means the system did something it did not
  intend, and that number should be zero.
- **Re-running a request is a new run.** It raises a new Material Request
  and a new Purchase Order. Before re-running anything, check whether the
  first run already submitted an order (section 11).

---

## 1. Cutover checklist

Work top to bottom. Do not start a section until the previous one is signed
off.

### Before

- [ ] ERPNext reachable; version recorded
- [ ] Company, fiscal year and currency match [CONFIGURATION.md](CONFIGURATION.md)
- [ ] `python seed/seed_erpnext.py --dry-run` reports only the `+` lines you expect
- [ ] `python seed/seed_erpnext.py`, then **run it again** — the second run must be all `=`
- [ ] Masters spot-checked in the UI: 6 suppliers each with a GSTIN in `tax_id`, 15 items, 1 warehouse, 15 prices on the buying list
- [ ] The `procureflow_key` custom field exists on Material Request, Purchase Order, Purchase Receipt and Purchase Invoice, and is unique, read-only and no-copy
- [ ] Stock Settings over-delivery allowance decided with the controller and recorded (CONFIGURATION.md §9)
- [ ] R12's table in the policy names the real approvers, and each can be reached out of hours
- [ ] API keys: the documented setup uses Administrator keys (CONFIGURATION.md §12). Record who holds them
- [ ] `PF_STATE_KEY` set if the CLI and the API run on different machines
- [ ] `.env` populated; `git status` clean — `.env` must not appear

### Dry run

- [ ] `make test` — every test passes
- [ ] `make eval` — every scenario passes and the baseline does not drift. A scratch run writes only git-ignored files (section 9)
- [ ] `procureflow run <file> --live --dry-run` ends `dry_run` with zero writes
- [ ] `procureflow permissions` shows `can_create_supplier: false` and `raw_http: false`
- [ ] With `PROCUREFLOW_WRITES=off`, `procureflow run seed/samples/req-happy.txt --live` ends `blocked` and nothing appears in ERPNext

### Approval routing

- [ ] An over-threshold request pauses, prints its order digest, and the Purchase Order stays at `docstatus: 0`
- [ ] `procureflow approve <run> --as <an id not in R12's table>` is refused and the run keeps waiting
- [ ] `procureflow approve <run> --as <an approver within their limit>` submits it
- [ ] `procureflow approve <run> --reject` escalates and leaves the order a draft

### Live

- [ ] One real low-value request, watched end to end
- [ ] All four documents verified **in the ERPNext UI**, not only in the trace: each carries a `procureflow_key`, and the invoice is a draft
- [ ] `procureflow verify-trace <run>` on that run says `intact`
- [ ] Rollback understood: delete the draft invoice, then cancel in reverse order (Receipt, Purchase Order, Material Request). An agent run creates nothing a buyer cannot undo by hand

### After

- [ ] Day 1: read every trace row
- [ ] Week 1: review the `needs_human` and `escalated` reasons — they are the backlog
- [ ] Week 2: revisit thresholds with the controller using real variance data
- [ ] Rotate the API keys when the implementation team hands over

---

## 2. Approvals

### Pause and approve

A request that needs approval (R1, or R14 or R15) stops at the gate:

```
  paused for approval. Order digest <first 12 characters>. Resume with:
      procureflow approve <run> --as <approver id>
      procureflow approve <run> --reject
```

The draft Purchase Order is in ERPNext at `docstatus: 0`. The run is on disk,
signed, in `eval/results/paused/`. The approver looks at the order and the
cited rules, then:

```bash
procureflow approve <run> --as proc.manager --note "ok, line 2 changeover"
```

`--as` takes an approver id from R12's delegation-of-authority table. The
order is read back from ERPNext, its digest compared with the one the gate
recorded, and the submit sent with the order's `modified` stamp. Once it is
submitted, the submitted order is checked against the approval once more.

Through the API, the approver's bearer token is the identity:
`POST /runs/<run>/approve` with a note in the body. Nothing in the body can
name a different approver.

### A refused approval

R12 refuses an approval that does not meet all three of its conditions. The
CLI prints the run with `state awaiting_approval` and a reason, and exits 1:

| Reason begins | Meaning | Next step |
|---|---|---|
| `approval not accepted: the approval does not name its approver` | `--as` was left out | approve again with `--as` |
| `... 'x' is not in the delegation-of-authority table` | not an approver | a named approver approves |
| `... raised this request and may not approve it (segregation of duties)` | the approver is the requester, by sign-off or by who submitted it | a different approver approves |
| `... exceeds the <role>'s authority of ...` | the order is above their limit | an approver with a higher limit approves |
| `... the requester is unknown, so segregation of duties cannot be checked` | the run was submitted with no identity and the document has no sign-off | re-run it with `--requester <who>`; nobody can approve this one |

**Why the run keeps waiting.** A refused approval is not a rejection. The
order is still waiting for a valid yes, so the run stays `awaiting_approval`
and is re-signed on disk with the refusal recorded. Any valid approver can
still approve it.

### Rejecting

```bash
procureflow approve <run> --reject --note "use the rate contract"
```

The run ends `escalated` and the order is never submitted. A rejection is
not checked against the table: stopping an order needs no authority.

The agent cannot delete documents, so a person cleans up in ERPNext:

- delete the draft Purchase Order. Until it is deleted, R15 counts it
  towards the same supplier's split-order total for seven days;
- close or cancel the Material Request if the demand has gone.

### The order changed while it waited

The run ends `blocked`: `Purchase Order <name> changed after it was approved
(<what changed>); the approval is void and the order needs approval again`.
The approval covers the supplier, every line's item, quantity, rate, unit,
warehouse and date, the grand total, the shipping address, set warehouse,
payment terms, currency and conversion rate. Any change voids it.

1. Open the order in ERPNext and read its version history: who changed it,
   and why.
2. If the change was wrong, that is the control working. Delete the draft.
3. If the change was right, delete the draft and re-run the request, so the
   approver sees what will actually be submitted.

### The submit was refused by the version lock

The run ends `needs_human`: `read-back verification failed: Purchase Order
<name> changed between its last read and the submit, so ERPNext refused
it`. Someone saved the order in the moment between the read-back and the
submit, and it is still a draft. Treat it as an order changed while it
waited (above).

A lost submit answer is not this. If the first submit went through and only
its answer was lost, the agent reads `docstatus`, finds the order submitted,
checks it against the approval and carries on.

### The order was submitted but is not what was approved

The run ends `needs_human`: `read-back verification failed: Purchase Order
<name> was submitted but does not match the order that was approved`. After
the submit, the submitted order's digest is checked against the approval
once more, and it differs. The order is **submitted** in ERPNext, and it is
not what the approver said yes to.

This is an incident. Switch writes off (section 5). Cancel the order in
ERPNext — the agent cannot — and read its version history to find who
changed it. **Do not re-run the request** until the order is cancelled;
that would raise a second order.

### The policy changed while the run was paused

The run ends `needs_human`: `R8: the policy changed while this run was
paused (version ... -> ...); re-run it under the current policy`. It was
gated under one rulebook and would have finished under another, so neither
the approval nor the earlier checks can be assumed to hold.

Delete the draft order and re-run the request. To avoid this, decide pending
approvals before a policy change goes live (section 8).

### A paused run that will not load

`refused: paused run <run> was modified after it was saved` (exit 3), or
`... is not in the signed format`. The file on disk is not the one the
application wrote, or it was signed with a different key. Treat an edited
file as an incident: keep a copy, do not delete it, and find out who had
write access to the folder. If the key simply differs between machines, set
the same `PF_STATE_KEY` on both.

---

## 3. Held invoices (R16)

The agent records every Purchase Invoice as a **draft**, and never submits
one. When the three-way match finds anything, the invoice is written with
`on_hold = 1` and a `hold_comment` such as:

```
ProcureFlow hold (policy 2.0): R3 price_variance LUB-GRS-EP2 +380.00
```

The run ends `completed_with_exceptions`, and cites R16 with the hold comment
as its detail, so the trace says a hold was placed and why. One entry per
exception, each with its rule:

| Exception | Rule | What to do |
|---|---|---|
| `price_variance` | R3 | query the rate with the supplier, or raise a PO amendment |
| `qty_short` | R4 | confirm the balance is still coming before paying in full |
| `qty_over` | R4 | confirm the extra was ordered before it is invoiced |
| `billed_over_received` | R11 | pay for what was received at most; ask for a credit note or wait for the balance |
| `unordered_line` | R11 | pay only for what was ordered; a genuine extra needs its own order |
| `missing_receipt` | R9 | chase Stores for the receipt |
| `possible_duplicate` | R5 | compare the two invoices with the supplier's statement before paying either a second time |

**A person releases the hold, in ERPNext.** When the exception is resolved,
someone with the authority clears `on_hold` on the invoice and records why.
The agent never does: it has no tool and no allowed server method that can
release a hold, and a test asserts that. Do not ask for one to be added.

Do not re-run the request to "clear" an exception. That raises a new order,
and R5 will block a second invoice with the same supplier number anyway.

---

## 4. Runs that stopped before anything was written

These all stop before the Material Request, so nothing in ERPNext needs
cleaning up.

### R13: quarantined

`needs_human`: `R13: the document was quarantined before any model read it
(<finding>)`. The finding names what was seen: an instruction to an
automated reader or to skip a control, text a person cannot see in the PDF,
or invisible characters. No model was called.

1. Open the original document and find the quoted evidence. The trace's
   `extract` step lists every finding.
2. Contact the requester through a channel you already know, not by
   replying to the document.
3. Ask for a clean copy, and run that. Do not strip the text yourself and
   re-run the same file: that launders an untrusted document.
4. If the finding was ordinary purchasing language caught by mistake, raise
   the order by hand and report the false positive so the pattern can be
   reviewed.

### R13: a request to change payment details

`escalated`: `R13: the document asks to change payment details; route to the
supplier-master owner for call-back verification`. This is the commonest
business-email-compromise move. The agent could not have acted on it — it
cannot touch a supplier — but a person must see that someone asked.

Route it to the supplier-master owner, who calls the supplier on a number
already on file, never one in the email. Raise the order only after that
call, from a clean copy.

### R13: a value that is not in the document

`needs_human`: `R13: extraction returned values that are not in the document:
line 1 quantity 5000`. The model read something the page does not say. It
was hallucinated or injected; either way it was not used. Raise the order by
hand from the document. If it recurs, look at the model and the document
format before anything else.

### R6: the supplier

| Reason | Next step |
|---|---|
| `supplier 'X' is not in the master; closest existing: ...` | onboard the supplier through the normal process, then re-run. The closest names are for a person to read; the agent will not act on them |
| `R6: supplier 'X' is disabled` / `is frozen` / `is on hold (...)` | the supplier-master owner decides. ERPNext would refuse the order anyway. Re-run after the standing changes or the release date passes |
| `R6: the document's GSTIN fails its check character` | treat the document as suspect; verify with the supplier on a known number |
| `R6: the document carries GSTIN X but the supplier master holds Y: the quote may not come from Z` | a possible impostor. Verify with the supplier on a known number. If the master is wrong, the master owner corrects `tax_id`, then re-run |

A payments-only hold does not stop a run; R6 is cited so the person paying
sees it.

**Do not** widen the write allowlist so the agent can create or edit
suppliers. That is the control R6 exists to keep.

### R14: the order cannot be valued

`needs_human`: `R14: the order cannot be valued, so R1 and R10 cannot be
checked: no stated rate and no list price for <item>`. Add an Item Price on
the buying list, or get a quote with a rate, and re-run.

### Extraction incomplete

`needs_human` citing R8: a missing quantity, or an item that did not resolve
to a catalogue code. Two codes matching equally well is a refusal to guess,
not a defect. Add the item, or add the requester's wording to the item
`description` so the next match is clear.

---

## 5. The kill switch

`PROCUREFLOW_WRITES=off` refuses every ERP write at the tool boundary. Reads
continue, dry runs are unaffected, and a run that reaches a write ends
`blocked`, citing R8: `writes are switched off (PROCUREFLOW_WRITES=off);
nothing was written`. `0` and `false` work too.

**Use it** on any read-back verification failure, any duplicate that was not
caught, an ERPNext that is rewriting documents, or a run of suspicious
documents.

**How.** It is read at every write, from the process's own environment.

- CLI: set it in the shell (`PROCUREFLOW_WRITES=off procureflow run ...`) or
  in `.env`. The next command picks it up.
- API: a running process cannot see a changed shell or `.env`. Restart it
  with the variable set.

**While writes are off, do not approve paused runs.** The resume reaches the
submit, ends `blocked`, and the paused run is discarded; the draft order
stays in ERPNext and the request has to be re-run. Leave them paused until
writes are back on.

To turn writes back on, unset the variable or set it to `on`, and restart
the API.

---

## 6. Verifying a trace

Every step row is hash-chained, the run's verdict (state, reason, cited
rules) is the last link, and the eval records each run's head in its
results file. A published tag's `results.json` is committed. See
[ARCHITECTURE.md](ARCHITECTURE.md#the-trace-one-row-per-step-hash-chained-anchored).

```bash
procureflow verify-trace <run>                                     # CLI or API run
procureflow verify-trace <run> --db eval/results/traces-eval.sqlite \
    --against eval/results/results.json                            # published eval run
procureflow verify-trace <run> --db eval/results/traces-eval.sqlite \
    --against eval/results/results-scratch.json                    # scratch eval run
```

`intact: <head>` (exit 0), or `BROKEN:` and where (exit 1):

| Output | Meaning |
|---|---|
| `BROKEN: step N (<node>) does not match its hash` | that step row was edited, or a row was deleted or reordered |
| `BROKEN: the run's verdict or chain head does not match its steps` | the run's state, reason or cited rules were edited, or the head was |
| `BROKEN: the chain is internally consistent but differs from the anchored head` | the whole chain was rewritten; only the committed record shows it |
| `BROKEN: run <run> has no anchored head in <file>` | `--against` was given a file that does not record this run, so nothing can be anchored |
| `BROKEN: no run <run>` | the run is not in that trace file |

Know its limits before relying on it:

- CLI and API runs have no anchor. Without `--against`, only the chain's
  internal consistency is checked. If you need an anchor for one, record
  its head somewhere the trace's editor cannot reach.
- The eval trace file is git-ignored and recreated on every eval run,
  `make eval` included. A published record can be verified only on the
  machine that produced it, and only until the next eval run there: verify
  it straight after publishing. The committed `eval/results/traces.json` is
  an export of the same rows.
- A trace written before the run's verdict joined the chain does not verify
  under the current code. That is a format change, not tampering.

A broken chain is an incident. Copy the trace file before anything else
touches it, and do not run the eval, which recreates its file.

---

## 7. Monitoring

Traces are in SQLite at `PF_TRACE_DB`: one row per run, one per step.

```sql
-- Runs that stopped for a person, and why. The daily list.
SELECT state, reason, COUNT(*) FROM runs
WHERE state IN ('needs_human','escalated','blocked','awaiting_approval')
GROUP BY state, reason ORDER BY 3 DESC;
```

```sql
-- Approvals R12 refused, and orders it voided.
SELECT run_id, outcome, detail FROM steps
WHERE node IN ('approval_gate','submit_purchase_order') AND outcome = 'refused';
```

```sql
-- Documents R13 quarantined or escalated.
SELECT run_id, state, reason FROM runs WHERE reason LIKE 'R13:%';
```

```sql
-- Invoices placed on hold (R16).
SELECT run_id, invoice_name, exceptions FROM runs WHERE rules_cited LIKE '%"R16"%';
```

```sql
-- Which policy each run was decided under.
SELECT policy_version, substr(policy_sha256, 1, 12), COUNT(*) FROM runs GROUP BY 1, 2;
```

```sql
-- Where the time goes.
SELECT node, COUNT(*), AVG(ms), MAX(ms) FROM steps GROUP BY node ORDER BY 3 DESC;
```

**Watch these:**

| Signal | Act when |
|---|---|
| read-back verification failures | **ever.** One is an incident: switch writes off (section 5) |
| `failed` runs | ever. `failed` is a defect, not a decision |
| a `BROKEN` trace, or a paused run refused as modified | ever |
| `needs_human` rate | it rises and stays up against the first weeks' level: extraction or masters have drifted |
| R13 quarantines | a cluster from one sender or supplier |
| `extract_method = llm-retry` | it rises: the model or the document format changed |
| step latency | it rises across every node at once: that is ERPNext, not the agent |

There is no live baseline for these yet. Set the levels from the first
weeks of real runs, not from the eval.

---

## 8. Changing the policy

The policy document is the control. **Never** edit
`policy/procurement_policy.md` to make a failing run pass without the
controller's sign-off; a threshold changed to clear a backlog is an audit
finding.

1. **Decide pending approvals first.** A run paused under the old policy
   stops `needs_human` when it resumes under the new one.
2. **Edit the rule, and bump `**Version x.y**` in the header.** Every rule
   heading needs a row in the summary table, and rule ids must run without
   gaps; a test checks both.
3. **Keep each number in its bold phrase.** Numbers are read only from a
   rule's operative text, never its rationale, and only in the exact form
   the policy uses: `**above ₹50,000**` (R1), `**5%**` and
   `**₹500 per line**` (R3), `**2%**` and `**1 unit**` (R4),
   `**fewer than 2 calendar days**` (R7), `**₹5,00,000**` (R10), `**2 pt**`
   (R13), `**7 days**` (R15) and `**30 days**` (R5). Change the number
   inside the phrase, not the phrase. **R1's amount also appears in the rule
   summary table**; change both, because they must agree.
4. **Check the parse.** If a number cannot be found, or R1's text and table
   disagree, the agent refuses to start and names the rule — the CLI fails
   with `PolicyError: the policy did not parse cleanly: R1 approval
   threshold did not parse from the rule's operative text`, or similar. It
   never falls back to a default. Check before anyone else meets it:

   ```bash
   python -c "from procureflow.policy_rag import read_numbers, verify_numbers; print(verify_numbers(read_numbers()) or 'parse ok')"
   python -c "from procureflow.policy_rag import read_numbers; print(read_numbers())"
   ```

   The second command prints what was read; amounts are in paise
   (`5000000` is ₹50,000). The same check also complains if R2 does not
   name two companies, R12's table does not parse, no approver's limit
   reaches R10, or R10 is not above R1.
5. **Run the tests and the eval.** A changed threshold fails the tests that
   pin the shipped numbers, and may move scenarios. That is intended:
   update them, and the baseline (section 9), in the same commit, where a
   reviewer sees the change.
6. **Restart the API.** It reads the policy once per process. The CLI reads
   it on every command.

Every run records the policy version and hash it was decided under, so the
trace shows which rulebook applied.

---

## 9. The eval baseline

`eval/baseline.json` freezes every scenario's final state, exceptions, cited
rules, ERP writes and step count. A scenario can keep passing while doing one
more write; the baseline is what notices.

**Day to day**, and in CI:

```bash
make eval                                            # --tag scratch --check-baseline
python eval/run_eval.py --tag ci --check-baseline    # what CI runs
```

`--check-baseline` fails on any scenario failure, and on any drift from the
baseline, or a scenario missing from the run. `procureflow eval` also uses
the `scratch` tag, but does not check the baseline.

**Scratch, CI and live runs do not touch the committed record.** A run
tagged `scratch`, `ci` or `live` writes `results-<tag>.json` and `.md`,
`exceptions-<tag>.xlsx` and `traces-<tag>.json`, all git-ignored, and may be
repeated freely. Every eval run does recreate the eval trace file
(section 6).

**An intended change** to behaviour:

```bash
python eval/run_eval.py --tag scratch --write-baseline
git diff eval/baseline.json
```

Read the diff. `--write-baseline` records whatever the run did, failures
included. Commit the new baseline **in the same commit** as the change that
caused it, so the reviewer sees both together.

**Publishing a record:**

```bash
make eval-publish TAG=v4                             # --tag v4 --check-baseline
```

Only a published tag updates `results.json`, `results.md`,
`exceptions.xlsx` and `traces.json`, as well as writing `results-v4.json`
and `.md`. `results.json` is the trace anchor, so commit them together, and
verify a trace or two before the next eval run recreates the trace file.

**Tagged results are write-once.** `results-<tag>.json` and `.md` are a
record of what a version of the code did. A tag that already has a file is
refused unless `--overwrite` is given; `ci`, `scratch` and `live` may be
reused. v2 silently overwrote the v1 record by reusing its tag, so the
published v1 figure stood next to a file that said something else. Give
every release a new tag.

Quote figures only from a tagged `results-<tag>.md`, and always with what it
ran against. The current record is v3: 48/48 scenarios, 0 false-touchless of
33, 0 trace-invariant violations — against FakeERPClient, synthetic corpus,
with heuristic extraction. There is no live ERPNext result and no LLM
extraction result. `--live` runs the same scenarios against `ERPNEXT_URL`;
publish its numbers beside the fake ones, never in place of them. Against a
live site the harness does not inject faults, change master data or edit a
paused order, so the scenarios that rely on those will not behave as they do
against the fake.

`--only <text>` runs the scenarios whose name contains `<text>`.

---

## 10. Other failure modes

### `needs_human: the ERP refused: <message>`

ERPNext validation — most often a schedule date before the transaction date,
or a closed fiscal period. The message is the server's own, unwrapped from
its HTML. Act on it directly. If it mentions a period, accounts must open
it.

### `needs_human: the ERP refused: Limit Crossed ...`

A receipt above its order, refused at submit by ERPNext's over-delivery
allowance (CONFIGURATION.md §9). By then the Purchase Order is **submitted**
and the receipt is a draft. Stores corrects the receipt, or the controller
changes the allowance; then finish the receipt and invoice by hand. Do not
re-run the request: that raises a second order.

### `needs_human: read-back verification failed: ...`

The stored document differs from what was sent. This is an incident.
Switch writes off, find what is rewriting the document, and do not switch
them back on until it is understood.

| The reason mentions | Usually |
|---|---|
| `stored a total of` | a pricing rule, price list or default discount repriced the order |
| `stored lines` | lines swapped or converted on save |
| `on hold` | something cleared or set the hold on save |
| `was submitted but does not match the order that was approved` | see section 2: the order is submitted and must be cancelled by hand |
| a field name and two values | a server script, workflow or customisation rewriting it |

**Do not** relax the read-back to make it pass.

### `blocked: R10: ...`

Either a genuinely large order, or a misread quantity. **Check the quantity
first.** R10 exists because a quantity misread by three orders of magnitude
would otherwise reach an approver who is approving twenty things that
morning. If the value is real, raise the order by hand. `R10: Purchase Order
<name> was stored at ...` means ERPNext priced it above the ceiling; the
order stays a draft.

### `blocked: supplier invoice <number> is already booked as <invoice>`

R5. Confirm with the supplier which invoice stands. **Do not** change the
number to get past it.

### A run hangs or times out

Reads are retried on timeouts and 5xx with backoff. A create and a submit
are each sent once. After an ambiguous create, the agent looks the document
up by its key before re-sending, so a timeout does not book a duplicate;
after a lost submit answer, it reads `docstatus` instead of submitting again.
A run that still ends with a timeout is the server — usually ERPNext's
background workers. Check the queue container logs.

---

## 11. Support playbook

**Triage, in order:**

1. The trace (section 7) — is this one run or all of them?
2. `GET /health` — are the ERP and the LLM configured, and which policy
   rules are loaded?
3. The trace for the run — which node, which outcome, which rule.
4. The ERPNext UI — does each document exist, and at what `docstatus`?

**Escalate immediately:** any read-back verification failure, any `failed`
state, any duplicate that was *not* caught, any `BROKEN` trace, any paused
run refused as modified.

**Safe to re-run:** a run that stopped before anything was written (section
4), once the cause is fixed.

**Check before re-running anything else.** Look up the run's `po_name` in the
`runs` table, then that order in ERPNext. If it is submitted, a re-run raises
a second order: finish by hand instead, or cancel the order first.

`procureflow report` prints the last **eval**, not production runs.

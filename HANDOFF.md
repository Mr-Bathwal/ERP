# Handoff — ProcureFlow

You are picking up a working project from another session. Read this first.
Most of what looks like an obvious improvement here was already tried and
either kept or deliberately rejected, and the reasons are in `git log`. Each
v3 commit is one defect and its fix, with the reproduction in its message.

**Working directory:** `C:\Users\goura\Downloads\procureflow`
**Repository:** https://github.com/Mr-Bathwal/ERP (public, `main`)
**Original brief:** `C:\Users\goura\Downloads\PROMPT_ERP_P2P_AGENT.md` — still
the source of truth for scope, the cut order and the CV deliverable.
**CV deliverable (written, outside the repo):**
`C:\Users\goura\Downloads\ProcureFlow_CV_and_Pitch.md`

No credential is in this repository. `.env` is git-ignored; `.env.example`
is committed and documents every setting.

---

## 1. What this is and who it is for

**ProcureFlow** — an AI agent that takes a free-text purchase request or a
supplier quote PDF and runs **procure-to-pay** in **ERPNext**: Material
Request → Purchase Order → (human approval) → goods receipt → Purchase
Invoice → three-way match → holds in ERPNext and an Excel exception report.

Built as a portfolio project for two roles at **Prentis**, an AI lab building
computer-use agents for office work: **Product Engineer, AI & Agentic
Systems** and **ERP Implementation Specialist**.

### The spine of the pitch

> **A successful-looking action is not a completed action.** v2 applied this
> to the agent's own writes. v3 applies it everywhere trust is usually
> assumed: the order's stored value, retries, approvals, approvers, the
> documents the model reads, the supplier's identity and the audit trail.

The README's first table is that argument. Do not dilute it.

---

## 2. State

| | |
|---|---|
| Tests | **239 passing**, fully offline (`make test`; Hypothesis is a dev dependency) |
| Lint | `ruff` clean |
| Eval | **48 scenarios, 100%** (v3), **0 false-touchless of 33**, 0 trace-invariant violations; v2 23/23, v1 19/24 |
| CI | lint + tests + eval with `--check-baseline` against `eval/baseline.json` |
| Policy | v2.0, rules R1–R16, parsed fail-closed |
| Pushed | **no** — the v3 commits are local only (`git log origin/main..HEAD`); ask before pushing |

### The v2 → v3 story (lead with this in interviews)

I audited my own v2 the way an auditor would, then ran eight research agents
and a skeptic over the working tree. Every defect was reproduced before it was
fixed. The headline one: **a request line with no price was valued at ₹0**,
so an order worth ₹74,60,000 at list went under R1 *and* the R10 ceiling and
was submitted. The rest are listed in the README ("What v2 to v3 was").

Also found: the committed `results-v1.json` had been overwritten by a later
run reusing the tag. The real v1 record is restored from `e873005`, and tagged
results are now write-once.

---

## 3. Architecture, in one page

Eleven fixed nodes, one direction. The model reads documents; the graph
decides. Where each control lives:

| Node / module | Controls |
|---|---|
| `extract` + `screen.py` | R13: screen before any model reads (instructions, hidden PDF text, invisible Unicode, bank-change requests); ground after (every value on the page) |
| `validate_masters` | R6: exists, not disabled/frozen/on hold, GSTIN check character and master match |
| `policy_check` | R1, R14 (every line valued; quote vs price list), R15 (split orders), R2, R7 |
| `tools.create_purchase_order` | R10 on the payload and again on the **stored** total; line-by-line read-back |
| `approval_gate` (`approval.py`) | R12: authority table, segregation of duties (fails closed), SHA-256 digest of what the approver saw |
| `submit_purchase_order` | re-read, digest compare, **version-locked** PUT with ERPNext's `modified` |
| `create_purchase_invoice` | R5 tiers (block / possible_duplicate hold), R16 hold written with the invoice |
| `three_way_match` (`match.py`) | R3, R4, R9, R11 (invoice vs receipt, in money); the hold is read back |
| `tools.insert_exactly_once` | idempotency key in a unique custom field; reconcile before re-send |
| `tracing.py` | one row per step, hash chain, head anchored in `results.json`; `verify-trace` |
| `state_store.py` | paused runs HMAC-signed before unpickling |
| `api.py` | bearer-token identity, no self-approval flag, inbox-confined sources, narrow CORS |

`FakeERPClient` reproduces the ERPNext behaviours that break integrations:
naming series, server pricing, unique keys, `modified` checks, supplier holds
with release dates, over-receipt refusal, and faults that fire *after* commit.

---

## 4. Run it

```bash
make setup
make test
python eval/run_eval.py --tag scratch --check-baseline
python -m procureflow.cli run seed/samples/req-over-threshold.txt
python -m procureflow.cli approve <run_id> --as proc.manager --note "ok"
python -m procureflow.cli verify-trace <run_id>
python -m http.server 8123 --directory dashboard    # dashboard (needs http)
```

Changing behaviour on purpose: re-run with `--write-baseline` and commit the
new `eval/baseline.json` in the same commit, so the reviewer sees the drift.
A new published result needs a new `--tag`.

---

## 5. What remains

### Blocked on the machine — the one real blocker

**ERPNext is not running.** Docker is **not installed** in WSL Ubuntu, and
`sudo` needs Gourav's password. He must run once:

```bash
wsl -d Ubuntu -- bash -c 'sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER && sudo service docker start && sudo docker run --rm hello-world'
```

Then `frappe_docker` with `pwd.yml`, API keys in `.env`, `make seed` (now also
creates the `procureflow_key` custom fields and supplier `tax_id`s) and
`make eval-live`. **Publish live numbers alongside the fake ones.** Expect
first-run surprises; the fake is a double, not ERPNext.

### Also outstanding

1. **Push v3** (ask first; check `git fetch` — another session may have pushed).
2. **LLM key run.** Set `LLM_API_KEY`, run with a new tag, report `llm` separately.
3. **Supplier-invoice intake** (`procureflow invoice <pdf>`), so R3/R5/R11 run
   on real invoices; until then invoices are generated and numbered `SIM-…`.
4. **Least-privilege ERPNext roles** per stage and an Authorization Rule at
   R10; the documented setup still uses Administrator keys.
5. **Deploy the dashboard** (GitHub Pages from `/dashboard`), then the CV
   header may say "Live".
6. **Demo video** (shot list in the brief §4 Phase 6.6), then "Demo".
7. **Pin the repo.**
8. Computer-use receipt mode (`computer_use.py` raises) — first on the cut list.

Deliberately dropped after review: header/GST total matching (no real invoice
headers exist yet), population screens and Benford (corpus too small),
OpenTelemetry export, signed e-invoice QR verification. Reasons are in the
research notes summarised in the v3 commits.

---

## 6. Known-imperfect, disclosed not hidden

- Every result is against `FakeERPClient`, with heuristic extraction.
- Invoices are generated from the PO; there is no invoice intake.
- Screening covers PDFs and plain text, not HTML or MIME parts.
- R5 and R15 read then write without a lock (two concurrent runs could both pass).
- `preferred` is not a real ERPNext field; R2 names the suppliers in the policy.
- Single company, INR only, one buying price list.

---

## 7. Rules that are not negotiable

- **Integer paise everywhere**, via `Decimal`. No floats in the money path.
- **The model never computes**, and every value it returns must be on the page (R13).
- **The agent cannot create a Supplier**, call a non-allowlisted server method, or release a hold.
- **`submit_purchase_order` is reachable only through `approval_gate`.**
- **R10 sits below the gate** and is checked on the stored total too.
- **Fail closed**: a policy that does not parse, an unknown requester, an empty invoice key, a policy changed while paused — all stop.
- **Never invent a metric.** Every number comes from a tagged, write-once eval record.
- Never relax a `verify=` set, and never re-freeze the baseline to make CI pass without saying why in the same commit.

# Handoff — ProcureFlow

You are picking up a working project from another session. Read this first.
Most of what looks like an obvious improvement here was already tried and
either kept or deliberately rejected, and the reasons are in `git log`.

**Working directory:** `C:\Users\goura\Downloads\procureflow`
**Repository:** https://github.com/Mr-Bathwal/ERP (public, `main`)
**Original brief:** `C:\Users\goura\Downloads\PROMPT_ERP_P2P_AGENT.md` — still
the source of truth for scope, the cut order and the CV deliverable.

No credential is in this repository. `.env` is git-ignored; `.env.example`
is committed.

---

## 1. What this is and who it is for

**ProcureFlow** — an AI agent that takes a free-text purchase request or a
supplier quote PDF and runs the **procure-to-pay** flow in **ERPNext**:
Material Request → Purchase Order → (human approval) → goods receipt →
Purchase Invoice → three-way match → Excel exception report.

Built as a portfolio project for **two roles at Prentis**, an AI lab
building computer-use agents for office work (Hoffman/Pincus, CEO Ritankar
Das, ~$100M at ~$1B; customers in manufacturing and healthcare):

- **Product Engineer, AI & Agentic Systems** — the LangGraph agent, typed
  allowlisted tools, retrieval, evaluation harness, traces.
- **ERP Implementation Specialist** — `docs/REQUIREMENTS.md`,
  `CONFIGURATION.md`, `RUNBOOK.md`, real masters, idempotent seeding.

### The spine of the pitch

> **A successful-looking write is not a completed write.** HTTP 200 means
> the server accepted the request, not that the stored document says what
> you believe it says.

Every write is followed by a read-back comparison. `VerificationError` is
its own exception type and is never folded into a generic ERP error. This
matters most for **computer-use agents**, which is exactly Prentis's
product — so do not dilute it.

---

## 2. State

| | |
|---|---|
| Tests | **80 passing**, fully offline, ~3 s (`make test`) |
| Lint | `ruff` clean (`make lint`) |
| Eval | **23 scenarios, 100%** (v2); v1 was 79.2% (19/24) |
| CI | `.github/workflows/ci.yml` — lint + tests + eval |
| Pushed | yes, `main` in sync |
| Dashboard | `dashboard/index.html`, static, reads the committed JSON |

### The v1 → v2 story (this is the best thing to talk about)

The harness found **two genuine product bugs**, not scenario noise:

1. A rejected approval was evaluated after **every** node, so a pre-set
   rejection escalated the run right after `extract` — before
   `policy_check` ran. The trace said "no approval required" for an order
   that had just been refused.
2. The fix for (1) **silently did nothing**: `fn is self.n_approval_gate`
   is never true, because Python builds a *new bound-method object* on each
   attribute access. The run fell through to `submit_purchase_order`, which
   refused it — the defence-in-depth guard caught the consequence while the
   cause went unnoticed. `==` fixed it.

Three more failures were my scenarios being wrong (three quantity scenarios
used a ₹74,400 order, over the R1 ceiling, so they stopped at the gate
before the match ran). One scenario was deleted honestly:
`transient_error_is_retried` asserted retry behaviour `FakeERPClient`
cannot have — retries live in the HTTP transport — so it became two unit
tests against the real client with a stubbed transport. **That is why v2
has 23 scenarios and v1 had 24.**

---

## 3. Architecture, in one page

**Eleven nodes, one direction, one place it stops.**
`extract → validate_masters → policy_check → create_material_request →
create_purchase_order → approval_gate → submit_purchase_order →
goods_receipt → create_purchase_invoice → three_way_match → report`

The sequence is fixed, not chosen per turn by the model: you cannot receive
against an order you have not submitted, and ERPNext refuses you if you
try. The model's job is **reading documents**; the graph's job is never
reordering the steps and never skipping the gate.

**The ERP boundary is the key design decision.** `ERPClient` is a Protocol
with two implementations — `ERPNextClient` (REST) and `FakeERPClient`
(in-memory). Everything above has never been told which it holds. That is
why CI needs no ERP, no network and no keys, and why `--dry-run` is a
*different object* rather than a flag a new node can forget.

`FakeERPClient` is not a stub that always succeeds. It reproduces naming
series, server-recomputed amounts, mandatory-field validation, the
draft/submit lifecycle, the refusal to map from a draft, and it can be told
to fail on command.

**No threshold is in the code.** `policy/procurement_policy.md` is the
process; the agent retrieves it with ChromaDB embeddings and cites the rule
id on every decision. R3 and R4 combine their two limits *differently on
purpose* — price takes the **tighter** of 5% and ₹500, quantity the
**greater** of 2% and 1 unit. Both directions are tested. Do not "simplify"
them into the same shape.

### File map

| Path | What |
|---|---|
| `policy/procurement_policy.md` | **The process.** R1–R10, each with its reason. Start here. |
| `procureflow/graph.py` | The 11 nodes, the driver, LangGraph wiring |
| `procureflow/tools.py` | 8 typed allowlisted tools; dry-run and R10 enforced here |
| `procureflow/erp/client.py` | Protocol, REST client, retries, read-back verify |
| `procureflow/erp/fake.py` | The test double + `demo_company()` masters |
| `procureflow/match.py` | Integer paise, R3/R4 tolerances, three-way match |
| `procureflow/extract.py` | LLM path + deterministic parser + item resolution |
| `procureflow/policy_rag.py` | Rule parsing, retrieval, threshold extraction |
| `procureflow/tracing.py` | SQLite traces, secret redaction |
| `procureflow/report_excel.py` | 3-sheet exception workbook |
| `eval/run_eval.py` + `eval/scenarios/*.yaml` | The harness and 23 scenarios |
| `tests/test_procureflow.py` | All 80 tests |
| `dashboard/index.html` | Static dashboard reading the committed JSON |

---

## 4. Run it

```bash
cd C:\Users\goura\Downloads\procureflow
make setup        # or: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
make test         # 80 tests, ~3s
make eval         # 23 scenarios -> eval/results/
python -m procureflow.cli run seed/samples/req-happy.txt
```

Approval across two processes (the headline feature):

```bash
python -m procureflow.cli run seed/samples/req-over-threshold.txt
python -m procureflow.cli approve <run_id> --note "ok"
```

Dashboard (must be over HTTP — `fetch` is blocked on `file://`):

```bash
python -m http.server 8123 --directory dashboard
```

---

## 5. What remains

### Blocked on the machine — the one real blocker

**ERPNext is not running.** Everything so far is against `FakeERPClient`.
The brief's "never cut" list includes *real ERPNext writes with read-back
verification*, so this is the highest-value remaining task.

- Docker is **not installed**. WSL2 Ubuntu **is** present, with 7.1 GB free
  RAM and 949 GB disk — ample.
- `sudo` in WSL needs a password, which the agent cannot supply. **Gourav
  must run this once:**

```bash
wsl -d Ubuntu -- bash -c 'sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker $USER && sudo service docker start && sudo docker run --rm hello-world'
```

Then:

```bash
wsl -d Ubuntu -- bash -c 'cd ~ && git clone https://github.com/frappe/frappe_docker && cd frappe_docker && sudo docker compose -f pwd.yml up -d'
# ~5 min for site creation; watch the create-site container logs
# http://localhost:8080  Administrator / admin
```

Then generate API keys (*User → Administrator → API Access → Generate
Keys*), put them in `.env`, and run `make seed` then `make eval-live`.
**Publish the live numbers alongside the fake ones; do not replace them
silently.**

### Also outstanding

1. **LLM key.** Extraction currently runs the deterministic parser —
   `results.json` reports `extraction: ["heuristic"]`. The LLM path is
   implemented, schema-validated and retried, but was never exercised.
   Set `LLM_API_KEY` (Groq free tier, `llama-3.3-70b-versatile`) and re-run
   to get an `llm` run. **Report it separately. A heuristic number that is
   true beats a model number that is borrowed.**
2. **Computer-use mode (Phase 5).** `procureflow/computer_use.py` currently
   *raises* rather than silently falling back. Implement with browser-use /
   Playwright against the ERPNext UI for the goods-receipt step, then
   verify through REST. First item on the brief's cut list — drop it from
   every claim if it is not shipped.
3. **Deploy the dashboard.** GitHub Pages from `/dashboard` is the fastest
   route; Vercel if the brief's wording matters.
4. **CV deliverable (brief §6).** Not yet written. Two lengths (105–120
   words and 70–80), a 4-line case-study blurb, a 60-second Prentis pitch,
   5 interview Q&As. **Use only numbers from `eval/results/results.md`.**
   Formatting rules in the brief: no `→`, no `×`, no "30x" multipliers.
5. **Demo video.** Shot list is in the brief §4 Phase 6.6.
6. **Pin the repo** on the GitHub profile.

---

## 6. Known-imperfect, disclosed not hidden

- **Headline numbers are against the fake**, not a live ERPNext.
  `results.json` says so in its own `mode` field, and the README says so
  twice. Keep it that way.
- **`traces.json` was empty of scenario traces** until late in the session —
  the eval built its `Agent` without `trace_db`. Fixed; if the dashboard's
  row-expansion shows no steps, re-run `make eval` and re-copy the three
  JSON files into `dashboard/`.
- **The dashboard is a single static HTML file**, not the Next.js app the
  brief specifies. Chosen deliberately for speed: no build step, deploys to
  Pages instantly. Gourav's own stack (`solutionai-landing`) is Next.js 16 +
  React 19 + three.js + GSAP + Lenis if you want to port it.
- `preferred` is not a real ERPNext field. The two preferred suppliers are
  named in the policy (R2) and noted in `supplier_details`, rather than
  inventing a custom field a vanilla site would not have. Explained in
  `CONFIGURATION.md`.
- Single company, INR only, one buying price list.

---

## 7. Rules that are not negotiable

- **Integer paise everywhere.** No floats in the money path.
- **The model never computes.** Extraction returns strings; arithmetic is
  deterministic and tested.
- **The agent cannot create a Supplier.** `Supplier` is read-only in the
  allowlist, a test asserts it, and R6 exists because requests arrive as
  email from outside the company.
- **`submit_purchase_order` is reachable only through `approval_gate`** —
  asserted against the node graph, not by reading the code.
- **R10 sits below the gate** so approval cannot waive it. It bounds what a
  *bug* can do, not what a person decides.
- **Never invent a metric.** Every number in the README and the CV bullet
  must come from an eval run that actually executed.
- Never relax a `verify=` set to make a run pass.

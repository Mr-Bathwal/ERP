# Configuration — what is set up in ERPNext, and why

What an ERPNext implementer sets up for ProcureFlow v3, and the reason for
each setting. Most of it is created idempotently by
[`seed/seed_erpnext.py`](../seed/seed_erpnext.py); run it twice and nothing
duplicates. Where a setting is **not** made by the seed, this document says
so, because those are the ones a fresh site will be missing.

Two things about the seed to know before relying on it:

- It checks whether a record exists, and creates it if not. **It never
  updates a record that already exists.** A supplier created by hand before
  the seed keeps whatever `tax_id` it had.
- It does not create the company, the fiscal year, users, roles, Stock
  Settings or Accounts Settings. Those are set by hand, below.

---

## 1. Modules in scope

| Module | Used for | Doctypes |
|---|---|---|
| **Buying** | the order and the supplier side | Supplier, Supplier Group, Purchase Order |
| **Stock** | demand and receipt | Material Request, Purchase Receipt, Item, Item Group, Warehouse, UOM, Stock Settings |
| **Accounts** | the payable | Purchase Invoice, Price List, Item Price |

Selling, Manufacturing, HR and Projects are untouched. A narrow footprint is
a feature during an implementation: fewer modules enabled is fewer things
that can be configured wrongly, and a smaller surface to hand over.

## 2. Company

Set by hand.

| Setting | Value | Why |
|---|---|---|
| Company name | ProcureFlow Demo Pvt Ltd | must equal `PF_COMPANY` |
| Abbreviation | PFD | suffixes warehouse and account names (`Stores - PFD`); must equal `PF_ABBR` |
| Default currency | INR | single-currency scope; removes conversion as a variable |
| Country | India | drives the GST tax templates and the date format |
| Fiscal year | 2026-04-01 → 2027-03-31 | Indian FY, so period-end behaviour is realistic |

## 3. Warehouse

| Setting | Value | Why |
|---|---|---|
| Name | `Stores - PFD` | one receiving point keeps the three-way match unambiguous; must equal `PF_WAREHOUSE` |
| Is group | No | a group warehouse cannot receive stock |
| Company | ProcureFlow Demo Pvt Ltd | — |

Set as `default_warehouse` on every item, via `item_defaults`. **This is the
setting that most often breaks a fresh implementation**: without it, every
Material Request line needs a warehouse supplied by the caller, and the
agent's payload would carry a value that belongs in master data.

## 4. Suppliers — 6

| Supplier | Group | Preferred (R2) | GSTIN in `tax_id` |
|---|---|---|---|
| Bharat Fasteners Pvt Ltd | Raw Material | **yes** | 29AABCB4321K1ZR |
| Deccan Steel & Alloys | Raw Material | **yes** | 29AABCD1234E1Z4 |
| Kaveri Polymers Ltd | Raw Material | no | 29AAFCK5566L1ZV |
| Nilgiri Abrasives | Raw Material | no | 29AAACN1122P1ZD |
| Sahyadri Lubricants LLP | Raw Material | no | 29AAJFS3344Q1ZF |
| Konkan Electricals | Raw Material | no | 29AADCK7788M1ZJ |

The GSTINs are synthetic, but each passes its structure and check
character. The GSTINs printed on v2's sample quotes all failed their check
character, and nothing noticed, because nothing checked.

**On "preferred":** stock ERPNext has no such field. Rather than add a custom
field a fresh site would not have, the two preferred suppliers are named in
the policy document (R2) and noted in `supplier_details`, where a buyer sees
them. The agent reads the names from the policy, not from ERPNext.

> In a real implementation this would become a Supplier field plus a naming
> convention, decided with the controller. It is called out here because
> quietly inventing a field is how a demo stops matching production.

**The agent cannot create a supplier.** `Supplier` is in the client's read
allowlist and not the write allowlist, and a test asserts it. Supplier
creation is the control point for bank details and tax registration, and
requests arrive as email from outside the company.

### 4.1 The GSTIN lives in core `tax_id`

The seed writes each supplier's GSTIN into ERPNext's core `tax_id` field.
Core, because a vanilla site has it; a localisation app's field would make
the repo unrunnable without that app. The code reads a field named `gstin`
first if the site has one, and `tax_id` otherwise.

R6 uses it like this. If a request or quote prints a GSTIN, it must pass its
structure (state code, PAN entity type) and its check character, and it must
**equal the master's**. A quote with a real supplier's name and someone
else's registration stops for a person.

**Fill `tax_id` for every supplier.** With no GSTIN on the master, R6 can
only check the printed one's structure; it has nothing to compare it with.

### 4.2 The Supplier fields R6 reads

R6 reads the Supplier record before anything is written. These are core
ERPNext fields; the seed leaves them at their defaults, and the
supplier-master owner sets them.

| Field | Effect on a run |
|---|---|
| `disabled` = 1 | escalated (R6), nothing written |
| `is_frozen` = 1 | escalated (R6), nothing written |
| `on_hold` = 1, `hold_type` = All or Invoices, no `release_date` | escalated (R6), nothing written |
| `on_hold` = 1, `hold_type` = All or Invoices, `release_date` still ahead | escalated (R6) until that date |
| `on_hold` = 1, `hold_type` = All or Invoices, `release_date` passed | proceeds |
| `on_hold` = 1, `hold_type` = Payments | proceeds, with R6 cited so the person paying sees it |

Why read these first: v2 only asked whether the supplier existed. A held
supplier passed, a Material Request was submitted, and ERPNext then refused
the Purchase Order with a generic error. Reading the standing up front
turns that into a stated reason, before any document exists.

## 5. Items — 15

Manufacturing consumables and raw materials: fasteners, steel, polymers,
abrasives, lubricants and electricals.

| Field | Value | Why |
|---|---|---|
| `item_group` | Raw Material | one group keeps the demo legible |
| `stock_uom` | Nos, except `POL-ABS-GR` in Kg | a mixed UOM is deliberate — requests say "20 kg" and the match must not silently compare kilograms to pieces |
| `is_stock_item` | 1 | required for a Purchase Receipt to move stock |
| `is_purchase_item` | 1 | otherwise the item cannot appear on a PO |
| `item_defaults[].default_warehouse` | Stores - PFD | see §3 |

Codes follow `<GROUP>-<SPEC>`: `FAS-M8-HEX`, `STL-SHT-2MM`, `LUB-HYD-68`.
A readable scheme is what lets fuzzy resolution work at all — and the
deliberately near-identical `FAS-M8-HEX` / `FAS-M10-HEX` pair is what the
ambiguity test exercises.

## 6. The buying price list R14 reads

| Setting | Value |
|---|---|
| Name | Standard Buying (`PF_PRICE_LIST`) |
| Buying / Selling | buying only |
| Currency | INR |
| Item Prices | 15, one per item |

R14 reads `Item Price.price_list_rate` for every item on the list named by
`PF_PRICE_LIST`. It uses it twice: to value a line that states no rate, and
to check a rate that is stated. A line with neither stops the run. A stated
rate above the list by more than R3's tolerance sends the order to an
approver whatever its value.

**Keep this list current.** It is now a control, not a convenience. An
out-of-date list either sends honest orders to an approver or values an
unpriced line at a stale rate.

Rates span ₹1.80 (a washer) to ₹18,650 (a drum of hydraulic oil) **on
purpose**. R3's "tighter of 5% and ₹500" only demonstrably behaves at both
ends if the catalogue has both ends.

Read limits in the code, for a larger catalogue: R14 reads at most 1,000
Item Price rows for the list, and R5 and R15 read at most 200 invoices or
orders per supplier. Beyond those, the code needs changing.

## 7. Naming series

Left at ERPNext defaults: `MAT-MR-.YYYY.-`, `PUR-ORD-.YYYY.-`,
`MAT-PRE-.YYYY.-`, `ACC-PINV-.YYYY.-`.

Deliberately not customised, because the agent must never assume a document
name it chose. `FakeERPClient` reproduces the same shapes and a test asserts
the format, so code that assumes otherwise fails offline rather than against
a live instance.

## 8. Custom field: procureflow_key

The one custom field this project adds. The seed creates it on **Material
Request, Purchase Order, Purchase Receipt and Purchase Invoice**, named
`<doctype>-procureflow_key`.

| Property | Value | Why |
|---|---|---|
| Field name | `procureflow_key` | the agent writes `pf-<run id>-<step>` into it on every create |
| Type | Data | a short string |
| Unique | 1 | the database itself refuses a second document with the same key, so a retried create cannot book a duplicate |
| Read-only | 1 | nobody types a key by hand, and nobody edits one away |
| No-copy | 1 | stops ERPNext's mappers carrying a Material Request's key onto the Purchase Order made from it, which the unique index would then refuse |
| Print hide | 1 | it means nothing to a supplier |
| Insert after | `naming_series` | out of the way |

Why a field at all: a timeout on a create does not mean nothing was
written. After an ambiguous failure the agent looks the document up by this
key before it re-sends, and adopts it if it is there. Without the field
there is nothing to look up by, and the only safe choices are a duplicate or
a stop. See [ARCHITECTURE.md](ARCHITECTURE.md#exactly-once-writes).

## 9. Stock Settings: over-delivery allowance

**Not set by the seed.** Decide it with the controller and record the value.

ERPNext's Stock Settings `over_delivery_receipt_allowance` defaults to 0%.
At 0%, ERPNext refuses at submit any Purchase Receipt above its order
("Limit Crossed"). The run then stops `needs_human` with that reason, and
the draft receipt stays in ERPNext for Stores to correct.

The two settings answer different questions. The allowance decides whether
ERPNext **accepts** a receipt at all. R4 decides whether an accepted receipt
is an **exception**. With an allowance of 10%, 66 received against 60
ordered is accepted at submit and then raised by R4 as `qty_over`.

The eval states which it assumes. Scenario 07 (`qty_over_receipt`) assumes
10%. Scenario 48 (`over_receipt_refused_by_erpnext_default`) runs at the
default and shows the refusal. v2's fake accepted any over-receipt, so
scenario 07 passed offline in a state a live site refuses.

## 10. Purchase Invoice: on_hold and hold_comment (R16)

Core ERPNext fields; nothing to create. The integration user must be able to
set them when it creates an invoice.

| Field | Written by the agent | Value |
|---|---|---|
| `on_hold` | yes, on every invoice | 1 if the match raised any exception, otherwise 0 |
| `hold_comment` | yes, when held | `ProcureFlow hold (policy <version>): ` then, for each exception, its rule id, exception, item and variance |
| `release_date` | no | — |

The agent records every invoice as a **draft** and never submits one. It
places holds and never releases them: no tool and no allowed server method
can. Releasing a hold is a person's decision, made in ERPNext. See
[RUNBOOK.md](RUNBOOK.md#3-held-invoices-r16).

Why in ERPNext and not only in a report: an exception that lives in a
spreadsheet does not stop anyone. Anyone opening the invoice sees the hold
and the reason.

## 11. Approval authority (R12)

The delegation-of-authority table lives **in the policy**, in R12, next to
the thresholds it qualifies. It is parsed from there like every other
number. It is not configured in ERPNext.

| Approver id | Name | Role | Approves up to |
|---|---|---|---|
| `planning.lead` | R. Iyer | Production Planning Lead | ₹1,00,000 |
| `proc.manager` | A. Rao | Procurement Manager | ₹2,00,000 |
| `finance.head` | V. Shah | Head of Finance | ₹5,00,000 |

The approver id is what a person gives as `procureflow approve <run> --as
<id>`, and it is the identity an approver's token maps to in
`PF_API_TOKENS`. An identity not in this table cannot approve anything.

The agent refuses to start if the table does not parse, or if no approver's
limit reaches the R10 ceiling, because then an order R10 allows could need
an approval nobody can give.

ERPNext's own purchase approval workflow is deliberately **not** configured.
The gate is in the agent, where the policy citation is; two approval
mechanisms would be two sources of truth.

## 12. API access: the known gap

*User → Administrator → API Access → Generate Keys*, then
`Authorization: token <api_key>:<api_secret>`.

**The documented setup still uses Administrator API keys.** That is a known
gap, stated plainly. The agent's own allowlists (doctypes, three server
methods, no Supplier writes) limit what this code asks for. They do not
limit what the key could do if it leaked.

The recommended next step, **not built and not tested**: a dedicated
integration user with a custom role that can

- read Supplier, Item, Item Price, Warehouse, Company and UOM;
- create, read and submit Material Request, Purchase Order and Purchase
  Receipt;
- create and read Purchase Invoice, including `on_hold` and
  `hold_comment`, but not submit it;
- and nothing else: no Supplier writes, no Payment Entry, no deletes.

The seed would keep its own Administrator key, used only during setup. Until
that exists, treat the API key as an administrator credential: one holder,
rotated at handover.

The secret is read from the environment at call time, never written to a
file or a log, and the tracer redacts anything matching a credential by key
name or value shape. `.env` is git-ignored; `.env.example` is committed.

## 13. Environment variables

Every variable in [`.env.example`](../.env.example). Defaults are the
values in code when the variable is unset.

| Variable | Default | Read by | What it does |
|---|---|---|---|
| `ERPNEXT_URL` | `http://localhost:8080` | ERP client, seed | the ERPNext site |
| `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET` | empty | ERP client, seed | live runs and the seed refuse to start without both |
| `LLM_BASE_URL` | Groq's OpenAI-compatible URL | extraction | any OpenAI-compatible endpoint |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | extraction | — |
| `LLM_API_KEY` | empty | extraction | empty means the deterministic parser runs, labelled `heuristic` |
| `PF_COMPANY` | ProcureFlow Demo Pvt Ltd | every payload, seed | must equal the company in §2 |
| `PF_ABBR` | PFD | seed | must equal the company abbreviation |
| `PF_WAREHOUSE` | Stores - PFD | every line's warehouse, seed | must equal §3 |
| `PF_RUN_CEILING` | `50000000` (paise, i.e. ₹5,00,000) | nothing | **informational only**, kept so an existing `.env` does not break. R10 is enforced from the policy, and `procureflow permissions` and `GET /permissions` show R10 from the policy too. There is no second copy of the ceiling to drift |
| `PF_TRACE_DB` | `eval/results/traces.sqlite` | CLI and API runs | the trace file. The eval uses its own, `eval/results/traces-eval.sqlite` |
| `PF_PRICE_LIST` | Standard Buying | R14, seed | the buying price list of §6 |
| `PROCUREFLOW_WRITES` | `on` | every write, at call time | `off`, `0` or `false` refuses every ERP write (R8). See [RUNBOOK.md](RUNBOOK.md#5-the-kill-switch) |
| `PF_API_TOKENS` | empty | API | `token=identity` pairs, comma-separated. The identity is a person for `/runs` and an R12 approver id for `/approve`. Empty means every write endpoint returns 401 |
| `PF_CORS_ORIGINS` | `http://localhost:8123,http://127.0.0.1:8123` (`.env.example` sets the first) | API | origins allowed to call it from a browser: the dashboard |
| `PF_INBOX` | `seed/samples` | API | the only folder the API reads source documents from |
| `PF_STATE_KEY` | empty | paused-run store | the key that signs paused runs. Empty means one is generated into `eval/results/paused/.state.key`. Every process that approves must use the same key, so set it when the CLI and the API run on different machines |

Generate an API token with
`python -c "import secrets; print(secrets.token_urlsafe(24))"`.

## 14. Recommended, not set by the seed

- **Accounts Settings: check supplier invoice number uniqueness.** R5 reads
  the booked invoices and then writes, without a lock, so two runs at the
  same moment could both pass. ERPNext's own check is a second line of
  defence. It has not been exercised by this project's tests or eval, so do
  not assume it normalises numbers the way R5 does.

## 15. What was deliberately left alone

| Not configured | Why |
|---|---|
| Purchase approval workflow in ERPNext | the gate is in the agent, where the policy citation is. Two approval mechanisms would be two sources of truth |
| Custom fields beyond `procureflow_key` | a near-vanilla site must be able to run this repo; the one field exists because exactly-once needs it |
| Tax templates beyond India defaults | GST arithmetic is out of scope; quotes carry GST for realism only, and the match does not compare tax |
| Email integration | input is a file path (or a name in `PF_INBOX`), so a run is reproducible |
| Stock reconciliation / opening balances | receipts move stock from zero, which is fine for a match demo |

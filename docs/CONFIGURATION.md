# Configuration — what was set up in ERPNext, and why

Written while configuring, not reconstructed afterwards. Everything here is
created idempotently by [`seed/seed_erpnext.py`](../seed/seed_erpnext.py);
run it twice and nothing duplicates.

---

## 1. Modules in scope

| Module | Used for | Doctypes |
|---|---|---|
| **Buying** | the order and the supplier side | Supplier, Supplier Group, Purchase Order |
| **Stock** | demand and receipt | Material Request, Purchase Receipt, Item, Item Group, Warehouse, UOM |
| **Accounts** | the payable | Purchase Invoice, Price List, Item Price |

Selling, Manufacturing, HR and Projects are untouched. A narrow footprint is
a feature during an implementation: fewer modules enabled is fewer things
that can be configured wrongly, and a smaller surface to hand over.

## 2. Company

| Setting | Value | Why |
|---|---|---|
| Company name | ProcureFlow Demo Pvt Ltd | — |
| Abbreviation | PFD | suffixes warehouse and account names (`Stores - PFD`) |
| Default currency | INR | single-currency scope; removes conversion as a variable |
| Country | India | drives the GST tax templates and the date format |
| Fiscal year | 2026-04-01 → 2027-03-31 | Indian FY, so period-end behaviour is realistic |

## 3. Warehouse

| Setting | Value | Why |
|---|---|---|
| Name | `Stores - PFD` | one receiving point keeps the three-way match unambiguous |
| Is group | No | a group warehouse cannot receive stock |
| Company | ProcureFlow Demo Pvt Ltd | — |

Set as `default_warehouse` on every item, via `item_defaults`. **This is the
setting that most often breaks a fresh implementation**: without it, every
Material Request line needs a warehouse supplied by the caller, and the
agent's payload would carry a value that belongs in master data.

## 4. Suppliers — 6

| Supplier | Group | Preferred (R2) |
|---|---|---|
| Bharat Fasteners Pvt Ltd | Raw Material | **yes** |
| Deccan Steel & Alloys | Raw Material | **yes** |
| Kaveri Polymers Ltd | Raw Material | no |
| Nilgiri Abrasives | Raw Material | no |
| Sahyadri Lubricants LLP | Raw Material | no |
| Konkan Electricals | Raw Material | no |

**On "preferred":** stock ERPNext has no such field. Rather than add a custom
field a fresh site would not have — which would make the repo unrunnable
against a vanilla instance — the two preferred suppliers are named in the
policy document (R2) and noted in `supplier_details`, where a buyer sees
them. The agent reads the names from the policy, not from a custom field.

> In a real implementation this would become a Supplier custom field plus a
> naming convention, decided with the controller. It is called out here
> because quietly inventing a field is how a demo stops matching production.

**The agent cannot create a supplier.** `Supplier` is in the client's read
allowlist and not the write allowlist, and a test asserts it. Supplier
creation is the control point for bank details and tax registration, and
requests arrive as email from outside the company.

## 5. Items — 15

Realistic manufacturing consumables and raw materials across five groups:
fasteners, steel, polymers, abrasives, lubricants and electricals.

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

## 6. Price list

| Setting | Value |
|---|---|
| Name | Standard Buying |
| Buying / Selling | buying only |
| Currency | INR |
| Item Prices | 15, one per item |

Rates span ₹1.80 (a washer) to ₹18,650 (a drum of hydraulic oil) **on
purpose**. R3's "tighter of 5% and ₹500" only demonstrably behaves at both
ends if the catalogue has both ends.

## 7. Naming series

Left at ERPNext defaults: `MAT-MR-.YYYY.-`, `PUR-ORD-.YYYY.-`,
`MAT-PRE-.YYYY.-`, `ACC-PINV-.YYYY.-`.

Deliberately not customised, because the agent must never assume a document
name it chose. `FakeERPClient` reproduces the same shapes and a test asserts
the format, so code that assumes otherwise fails offline rather than against
a live instance.

## 8. API access

*User → Administrator → API Access → Generate Keys*, then
`Authorization: token <api_key>:<api_secret>`.

The secret is read from the environment at call time, never written to a
file or a log, and the tracer redacts anything matching a credential by key
name or value shape. `.env` is git-ignored; `.env.example` is committed.

## 9. What was deliberately left alone

| Not configured | Why |
|---|---|
| Purchase approval workflow in ERPNext | the gate is in the agent, where the policy citation is. Two approval mechanisms would be two sources of truth. |
| Custom fields | a vanilla site must be able to run this repo |
| Tax templates beyond India defaults | GST arithmetic is out of scope; quotes carry GST for realism only |
| Email integration | input is a file path, so the demo is reproducible |
| Stock reconciliation / opening balances | receipts move stock from zero, which is fine for a match demo |

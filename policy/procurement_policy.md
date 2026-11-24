# Procurement Policy — ProcureFlow Demo Pvt Ltd

**Version 1.4** · Effective 2026-11-24 · Owner: Head of Supply Chain
**Applies to:** all indirect and raw-material purchasing routed through ERPNext.

This document is the process. ProcureFlow holds no thresholds in code — the
agent retrieves the rules below and must cite the rule id behind every
decision it records. If a number here changes, the agent's behaviour changes
on the next run with no code change and no redeploy.

Each rule is written so that it can be checked mechanically. A rule that
cannot be evaluated against a document is a guideline, and guidelines belong
in the handbook, not here.

---

## R1 — Approval threshold

A Purchase Order with a grand total **above ₹50,000** requires manager
approval before submission. At or below ₹50,000 the agent may submit
automatically and must record R1 as the rule that permitted it.

> **Why this number.** It is the point at which a mistake costs more than the
> twenty minutes of a buyer's attention that checking it takes. It is not an
> industry standard and no ERP ships it as a default; it was set by the Head
> of Supply Chain in April 2026 against the prior year's spend distribution,
> where 71% of lines fell below it.

**Evaluated on:** Purchase Order `grand_total`, before tax, in INR.

---

## R2 — Preferred suppliers

Where a preferred supplier stocks the item, the order must go to a preferred
supplier. Preferred suppliers are **Bharat Fasteners Pvt Ltd** and **Deccan
Steel & Alloys**.

If a request names a non-preferred supplier for an item a preferred supplier
stocks, the agent proposes the substitution, records R2, and continues. It
does **not** substitute silently: the original request and the substitution
are both kept on the run.

> **Why.** Both carry negotiated rate contracts and 30-day terms. The saving
> is real but small; the reason the rule exists is that unmanaged supplier
> sprawl is what makes the master data unusable two years later.

---

## R3 — Price variance tolerance

An invoiced rate may exceed the Purchase Order rate by at most **5%**, or
**₹500 per line**, whichever is **tighter**. Anything beyond that is a price
variance exception and goes to a human.

> **Why both a percentage and a cash cap.** A percentage alone is meaningless
> at the extremes: 5% of a ₹28 pair of gloves is ₹1.40, which costs more to
> investigate than to pay, while 5% of a ₹18,650 drum of hydraulic oil is
> ₹932 of unbudgeted spend waved through. The tighter of the two binds, so
> the rule behaves sensibly at both ends of the catalogue.

**Evaluated on:** Purchase Invoice item `rate` against Purchase Order item
`rate`, per line.

---

## R4 — Quantity variance tolerance

Received quantity may differ from ordered quantity by at most **2%**, or
**1 unit**, whichever is **greater**. Beyond that:

- received **less** than ordered → `qty_short`
- received **more** than ordered → `qty_over`

> **Why "greater" here and "tighter" in R3.** Quantity variance at small
> counts is almost always a packaging or count-rounding artefact, not a
> commercial event: 2% of 10 units is 0.2 of a unit, which would make every
> single-unit discrepancy an exception. Money is the opposite case. The two
> rules disagree on purpose.

**Evaluated on:** Purchase Receipt item `qty` against Purchase Order item
`qty`, per line.

---

## R5 — Duplicate supplier invoices

A supplier invoice number that already exists against the same supplier must
be **blocked**. Comparison ignores case, spaces and punctuation, so
`INV-0042`, `inv 0042` and `INV0042` are one invoice.

> **Why normalise.** Duplicate payment is the single most common avoidable
> loss in accounts payable, and the commonest cause is the same invoice
> re-keyed with different punctuation. An exact-string check finds only the
> duplicates that were never a risk.

**This rule can never be auto-approved**, regardless of value. See R8.

---

## R6 — Unknown supplier

If the supplier on a request does not exist in the ERPNext Supplier master,
the agent **stops and escalates**. It must not create the supplier, and must
not guess at the nearest existing name.

> **Why.** Supplier creation is the control point for bank details, tax
> registration and onboarding checks. An agent that can create a supplier is
> an agent that can be talked into creating a payee, and the request arrives
> as an email from outside the company.

---

## R7 — Expedite flag

A needed-by date **fewer than 2 calendar days** from the request date raises
an expedite flag. The order proceeds, but the flag is recorded on the run and
appears in the exception report.

> **Why it does not block.** Genuine line-stop situations are exactly when a
> procurement process must not add friction. The flag exists so that the
> pattern is visible monthly, not so that one order is delayed.

---

## R8 — Decisions that are never automatic

The following are never auto-approved, whatever the value and whatever other
rules permit:

- `R5` duplicate supplier invoice
- `R6` unknown supplier
- any run where extraction returned `needs_human`
- any run where read-back verification failed

> **Why this list is separate.** Every other rule asks whether a document is
> *correct*. These ask whether proceeding is *allowed at all* — a question of
> authority, not arithmetic. A clean three-way match is evidence that an
> invoice is right; it is not permission to pay it.

---

## R9 — Missing goods receipt

An invoice with no corresponding Purchase Receipt is a `missing_receipt`
exception. The invoice is held, not rejected: in practice the receipt usually
arrives within two working days and the hold clears itself.

> **Why hold rather than reject.** Rejecting pushes the problem to the
> supplier, who cannot fix it — the goods are in our building and the paper
> is ours. Four major ERPs each ship a dedicated hold for this state rather
> than treating it as a dispute.

---

## R10 — Order value ceiling per run

No single automated run may create a Purchase Order with a grand total above
**₹5,00,000**, even with approval. Above that, the run stops and the order is
raised by a buyer by hand.

> **Why a ceiling on top of an approval gate.** An approval gate protects
> against a wrong decision; a ceiling protects against a wrong *system*. If
> ProcureFlow ever misreads a quantity by three orders of magnitude, R1 would
> dutifully route a ₹40,00,000 order to a manager who is approving twenty
> things that morning. R10 means the blast radius of a bug is bounded by
> something that is not a human's attention.

---

## R11 — Billed quantity and unordered lines

An invoice may bill an item for **at most the quantity received**, within the
same allowance R4 gives a receipt. Beyond that the line is
`billed_over_received` and payment for it is held. An invoice line for an
item that is **not on the Purchase Order** is `unordered_line` and is held.

> **Why a third leg.** R3 compares the invoice with the order and R4 compares
> the receipt with the order. Neither compares the invoice with the receipt,
> so a supplier who ships 45 of 60 and bills 60 raised a delivery exception
> and nothing said the bill itself was wrong. A line nobody ordered —
> freight, handling, "miscellaneous" — was not compared with anything at
> all. Paying for goods that did not arrive, and for charges nobody
> approved, is exactly the loss a three-way match exists to prevent.

**Evaluated on:** Purchase Invoice item `qty` against Purchase Receipt item
`qty`, per item; and every Purchase Invoice item against the Purchase Order.

---

## Rule summary

| Rule | Subject | Threshold | Effect when breached |
|---|---|---|---|
| R1 | PO approval | > ₹50,000 | human approval required |
| R2 | Preferred supplier | item stocked by preferred | propose substitution |
| R3 | Price variance | 5% or ₹500, tighter | `price_variance` |
| R4 | Qty variance | 2% or 1 unit, greater | `qty_short` / `qty_over` |
| R5 | Duplicate invoice | normalised match | blocked, never automatic |
| R6 | Unknown supplier | not in master | escalate, never automatic |
| R7 | Expedite | needed in < 2 days | flag, continue |
| R8 | Authority | — | never automatic |
| R9 | Missing receipt | no receipt for invoice | `missing_receipt`, hold |
| R10 | Run ceiling | > ₹5,00,000 | stop, raise by hand |
| R11 | Billed vs received | R4 allowance on receipt | `billed_over_received` / `unordered_line`, hold |

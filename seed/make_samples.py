"""Generate the synthetic inputs the agent reads.

Everything here is invented. No real supplier, person or document is used,
and the README says so. What is *not* invented is the shape of the mess:
these files reproduce the ways real purchase requests actually arrive —
quantities written as words, units that disagree with the catalogue, two
items crammed into one sentence, a quote PDF whose line table wraps, a
request with no quantity at all.

A clean corpus would make the evaluation meaningless. The point of the
harness is to find out what fraction of realistic inputs the agent handles
and what it does with the rest, so the corpus has to contain inputs it
should *refuse*: `req-noqty` has no quantity and must end `needs_human`,
`req-unknown-supplier` names a company that is not in the master and must
escalate. A scenario that the agent passes by stopping is still a pass.

    python seed/make_samples.py
"""

from __future__ import annotations

import textwrap
from pathlib import Path

OUT = Path(__file__).resolve().parent / "samples"

# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

EMAILS: dict[str, str] = {

    "req-happy": """\
        Subject: Stores replenishment - fasteners

        Hi,

        Please raise a PO on Bharat Fasteners Pvt Ltd for the following:

        - 500 nos Hex bolt M8x40 stainless @ 12.00
        - 200 nos Flat washer M8 zinc @ 1.80

        Needed by 2026-12-15.

        Thanks,
        R. Iyer
        Production Planning
        """,

    "req-over-threshold": """\
        Subject: Hydraulic oil - line 2 changeover

        Please raise an order on Deccan Steel & Alloys.

        - 4 nos Hydraulic oil ISO VG68 210L @ 18650.00

        Needed by 2026-12-20. This is for the line 2 changeover so it
        cannot slip.

        S. Nambiar
        Maintenance
        """,

    # R12: Rs2,23,800 is inside R10 but above the Procurement Manager's
    # Rs2,00,000 authority, so only the Head of Finance may approve it.
    "req-authority-limit": """\
        Subject: Hydraulic oil - annual top-up for the press shop

        Please raise an order on Deccan Steel & Alloys.

        - 12 nos Hydraulic oil ISO VG68 210L @ 18650.00

        Needed by 2027-01-05.

        J. Fernandes
        Press Shop
        """,

    "req-messy-typos": """\
        Subject: urgent plz

        hi team

        we need some stuff from Bharat Fasteners Pvt Ltd asap

        - 120 nos hex bolt m10x50 stainles @ 19.50
        - 60 pcs flat washer m8 zinc

        needed by 2026-12-09

        thnks
        p. deshmukh
        """,

    "req-noqty": """\
        Subject: Abrasives

        Hi,

        Could you order flap discs from Nilgiri Abrasives please. We are
        running low on the 100mm ones.

        Needed by 2026-12-18.

        K. Rao
        """,

    "req-unknown-supplier": """\
        Subject: New vendor order

        Please raise a PO on Vindhya Tooling Solutions Pvt Ltd for:

        - 30 nos Cutting wheel 230mm @ 48.00

        Needed by 2026-12-22.

        A. Menon
        """,

    "req-expedite": """\
        Subject: LINE DOWN - need today

        Bharat Fasteners Pvt Ltd

        - 50 nos Hex bolt M8x40 stainless @ 12.00

        Needed by 2026-11-25. Line is stopped.

        T. Pillai
        """,

    "req-two-in-one-line": """\
        Subject: Consumables

        From Kaveri Polymers Ltd we need 20 nos ABS granules natural 25kg
        and also 12 nos PVC sheet 3mm 1000x2000, both by 2026-12-19.

        Regards
        M. Shetty
        """,

    "req-nonpreferred": """\
        Subject: Steel sheet

        Please order from Konkan Electricals:

        - 8 nos Mild steel sheet 2mm 1250x2500 @ 4180.00

        Needed by 2026-12-28.

        V. Krishnan
        """,

    "req-short-delivery": """\
        Subject: Bright rod stock

        Supplier: Deccan Steel & Alloys

        - 60 nos Bright steel rod 20mm 6m @ 1240.00

        Needed by 2026-12-21.

        R. Iyer
        """,

    "req-price-variance": """\
        Subject: Grease order

        Supplier: Bharat Fasteners Pvt Ltd

        - 6 nos EP2 lithium grease 18kg @ 4320.00

        Needed by 2026-12-24.

        S. Nambiar
        """,

    "req-ceiling-breach": """\
        Subject: Annual oil stock

        Supplier: Deccan Steel & Alloys

        - 40 nos Hydraulic oil ISO VG68 210L @ 18650.00

        Needed by 2027-01-10. Bulk buy for the year.

        Procurement
        """,

    "req-ambiguous-item": """\
        Subject: Bolts

        Supplier: Bharat Fasteners Pvt Ltd

        - 100 nos hex bolt @ 12.00

        Needed by 2026-12-30.

        K. Rao
        """,
}


# ---------------------------------------------------------------------------
# Quote PDFs
# ---------------------------------------------------------------------------

QUOTES: dict[str, dict] = {
    "quote-deccan-multi": {
        "supplier": "Deccan Steel & Alloys",
        "quote_no": "DSA/Q/2026/4417",
        "date": "2026-11-20",
        "valid": "2026-12-31",
        "gstin": "29AABCD1234E1Z5",
        "needed_by": "2026-12-26",
        "lines": [
            ("Mild steel sheet 2mm 1250x2500", 10, "Nos", 4180.00),
            ("Bright steel rod 20mm 6m", 25, "Nos", 1240.00),
            ("MS angle 50x50x5 6m", 18, "Nos", 980.00),
        ],
    },
    "quote-kaveri-polymers": {
        "supplier": "Kaveri Polymers Ltd",
        "quote_no": "KPL-2026-0912",
        "date": "2026-11-21",
        "valid": "2026-12-20",
        "gstin": "29AAFCK5566L1ZP",
        "needed_by": "2026-12-27",
        "lines": [
            ("ABS granules natural 25kg", 40, "Kg", 182.00),
            ("PVC sheet 3mm 1000x2000", 15, "Nos", 740.00),
        ],
    },
    "quote-konkan-electricals": {
        "supplier": "Konkan Electricals",
        "quote_no": "KE/26/1188",
        "date": "2026-11-22",
        "valid": "2026-12-15",
        "gstin": "29AADCK7788M1ZQ",
        "needed_by": "2026-12-29",
        "lines": [
            ("Copper cable 2.5sqmm 90m", 6, "Nos", 3150.00),
            ("MCB 32A C-curve", 24, "Nos", 410.00),
        ],
    },
}


def write_emails() -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    out = []
    for name, body in EMAILS.items():
        p = OUT / f"{name}.txt"
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        out.append(p)
    return out


def write_quotes() -> list[Path]:
    """Render the quotes as real PDFs with reportlab.

    Real PDFs, not text files renamed: the extractor's PDF path goes through
    pypdf, and a text layer produced by a PDF generator does not come out in
    the order a human reads it. Testing against a fake PDF would hide that.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    OUT.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    out = []

    for name, q in QUOTES.items():
        path = OUT / f"{name}.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4,
                                leftMargin=18 * mm, rightMargin=18 * mm,
                                topMargin=18 * mm, bottomMargin=18 * mm,
                                title=f"Quotation {q['quote_no']}")
        story = [
            Paragraph(f"<b><font size=15>{q['supplier']}</font></b>", styles["Normal"]),
            Paragraph(f"GSTIN {q['gstin']}", styles["Normal"]),
            Spacer(1, 10 * mm),
            Paragraph("<b>QUOTATION</b>", styles["Heading2"]),
            Paragraph(f"Quote No: {q['quote_no']}<br/>"
                      f"Date: {q['date']}<br/>"
                      f"Valid until: {q['valid']}<br/>"
                      f"Delivery required by: {q['needed_by']}", styles["Normal"]),
            Spacer(1, 6 * mm),
            Paragraph("To: ProcureFlow Demo Pvt Ltd, Bengaluru", styles["Normal"]),
            Spacer(1, 6 * mm),
        ]

        rows = [["#", "Description", "Qty", "UOM", "Rate (INR)", "Amount (INR)"]]
        subtotal = 0.0
        for i, (desc, qty, uom, rate) in enumerate(q["lines"], 1):
            amt = qty * rate
            subtotal += amt
            rows.append([str(i), desc, f"{qty:g}", uom, f"{rate:,.2f}", f"{amt:,.2f}"])

        gst = round(subtotal * 0.18, 2)
        rows += [["", "", "", "", "Subtotal", f"{subtotal:,.2f}"],
                 ["", "", "", "", "GST 18%", f"{gst:,.2f}"],
                 ["", "", "", "", "Total", f"{subtotal + gst:,.2f}"]]

        t = Table(rows, colWidths=[10 * mm, 72 * mm, 16 * mm, 16 * mm, 28 * mm, 30 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2328")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -4), 0.4, colors.HexColor("#d1d9e0")),
            ("LINEABOVE", (4, -3), (-1, -3), 0.6, colors.HexColor("#1f2328")),
            ("FONTNAME", (4, -1), (-1, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story += [t, Spacer(1, 8 * mm),
                  Paragraph("Terms: 30 days nett. Prices ex-works Bengaluru. "
                            "E&OE.", styles["Normal"]),
                  Paragraph("<i>Synthetic document generated for the ProcureFlow "
                            "demo. No real supplier is represented.</i>",
                            styles["Italic"])]
        doc.build(story)
        out.append(path)
    return out


def main() -> None:
    emails = write_emails()
    quotes = write_quotes()
    print(f"wrote {len(emails)} emails and {len(quotes)} quote PDFs to {OUT}")
    for p in emails + quotes:
        print("  ", p.name)


if __name__ == "__main__":
    main()

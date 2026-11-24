"""R13: a document is data, never instructions.

Every request and quote this agent reads arrives from outside the company,
by email. The model that reads it cannot tell a sentence it should extract
from a sentence that is trying to steer it, and asking it nicely to ignore
instructions is not a control. So two deterministic checks sit either side
of the model:

**Before it reads (`screen`).** The raw document is checked for:

- text addressed to an automated reader, or asking for a control to be
  skipped ("ignore previous instructions", "pre-approved by finance", "do
  not flag");
- text a human reviewer cannot see: PDF text in invisible render mode, in a
  microscopic font, or white text that is not drawn over a dark shape; and
  invisible Unicode (zero-width and bidirectional controls);
- a request to change payment details -- the commonest business-email-
  compromise move, and something this agent could not act on anyway, but
  which a person must see.

A finding stops the run *before* any model sees the document. The agent's
tools are already incapable of the worst outcomes (it cannot create a
supplier or touch bank details); this stops a document from even trying.

**After it reads (`ground`).** Every value extraction returned must be
traceable to the document: the supplier, each line's description, quantity
and rate, the needed-by date and the supplier's reference. A number that is
not on the page was hallucinated or injected, and is never used. This turns
"the model never computes" into something checked rather than hoped.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .match import dec, normalise_invoice_no

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str        # instruction | hidden_text | invisible_chars | payment_change
    evidence: str    # the offending text, shortened

    def __str__(self) -> str:
        return f"{self.kind.replace('_', ' ')}: {self.evidence!r}"


_INSTRUCTION = re.compile(
    r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|existing|system)\s+(?:instructions?|prompts?|rules?)"
    r"|\b(?:ai|automated|llm|language\s+model|bot|agent|assistant)\b[^.\n]{0,40}"
    r"\b(?:must|should|shall|please|instructed|note)\b"
    r"|\bnote\s+to\s+the\s+(?:ai|assistant|agent|system|model)\b"
    r"|\bpre[-\s]?approved\s+by\b"
    r"|\b(?:skip|bypass|without)\s+(?:the\s+|any\s+)?(?:approval|review|verification|checks?)\b"
    r"|\bdo\s+not\s+(?:flag|escalate|check|verify|hold|query)\b"
    r"|\bmark\s+(?:this|it|the\s+order)\s+(?:as\s+)?approved\b"
    r"|^\s*(?:system|assistant)\s*:"
    r"|<\|im_start\|>|\[/?INST\]|###\s*instruction",
    re.IGNORECASE | re.MULTILINE)

_PAYMENT_CHANGE = re.compile(
    r"\b(?:new|updated|changed|revised|different)\s+(?:bank|account|remittance|beneficiary|"
    r"payment)\s*(?:details?|account|number|information)?"
    r"|\b(?:bank|account|remittance|beneficiary|ifsc|iban|swift)\b[^.\n]{0,50}"
    r"\b(?:has|have)\s+(?:been\s+)?(?:changed|updated)",
    re.IGNORECASE)

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def _clip(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


def _line(text: str, start: int, end: int) -> str:
    """The whole line(s) a match sits in: evidence a reviewer can find."""
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    return text[a:b if b != -1 else len(text)]


def screen_text(text: str) -> list[Finding]:
    """Instruction-like, payment-changing and invisible content in plain text."""
    out: list[Finding] = []
    for m in _INSTRUCTION.finditer(text):
        out.append(Finding("instruction", _clip(_line(text, m.start(), m.end()))))
    for m in _PAYMENT_CHANGE.finditer(text):
        out.append(Finding("payment_change", _clip(_line(text, m.start(), m.end()))))
    if (chars := _INVISIBLE.findall(text)):
        names = sorted({unicodedata.name(c, hex(ord(c))) for c in chars})
        out.append(Finding("invisible_chars", ", ".join(names)))
    seen: set[tuple[str, str]] = set()
    return [f for f in out if not ((f.kind, f.evidence) in seen or seen.add((f.kind, f.evidence)))]


# ---------------------------------------------------------------------------
# Text a human cannot see, in a PDF
# ---------------------------------------------------------------------------


def _is_white(op: bytes, args: list[Any]) -> bool | None:
    try:
        vals = [float(a) for a in args]
    except (TypeError, ValueError):
        return None
    if op == b"rg" and len(vals) == 3:
        return all(v >= 0.95 for v in vals)
    if op == b"g" and len(vals) == 1:
        return vals[0] >= 0.95
    if op == b"k" and len(vals) == 4:
        return all(v <= 0.05 for v in vals)
    return None


def _apply(m: list[float], x: float, y: float) -> tuple[float, float]:
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def _shown(args: list[Any]) -> str:
    parts: list[str] = []
    for a in args:
        if isinstance(a, list):
            parts.extend(str(x) for x in a if isinstance(x, (str, bytes)))
        elif isinstance(a, (str, bytes)):
            parts.append(a.decode("latin-1") if isinstance(a, bytes) else str(a))
    return "".join(parts)


Box = tuple[float, float, float, float]


class _PageScan:
    """Walks one page's content stream, tracking just enough graphics state.

    Fill colour, text render mode and font size live in the graphics state,
    which `q`/`Q` save and restore; filled rectangles are remembered so that
    white text can be tested against what it is drawn on.
    """

    def __init__(self, min_font_pt: float) -> None:
        self.min_font_pt = min_font_pt
        self.st: dict[str, Any] = {"white": False, "render": 0, "size": 12.0}
        self.stack: list[dict[str, Any]] = []
        self.pending: list[Box] = []
        self.dark: list[Box] = []
        self.hidden: list[str] = []

    def visit(self, op: bytes, args: list[Any], cm: list[float], tm: list[float]) -> None:
        st = self.st
        if op == b"q":
            self.stack.append(dict(st))
        elif op == b"Q" and self.stack:
            st.update(self.stack.pop())
        elif op in (b"rg", b"g", b"k"):
            if (w := _is_white(op, args)) is not None:
                st["white"] = w
        elif op == b"Tr" and args:
            st["render"] = int(args[0])
        elif op == b"Tf" and len(args) >= 2:
            st["size"] = float(args[1])
        elif op == b"re" and len(args) == 4:
            x, y, w, h = (float(a) for a in args)
            xs, ys = zip(*(_apply(cm, px, py) for px, py in
                           ((x, y), (x + w, y), (x, y + h), (x + w, y + h))), strict=True)
            self.pending.append((min(xs), min(ys), max(xs), max(ys)))
        elif op in (b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
            if not st["white"]:
                self.dark.extend(self.pending)
            self.pending = []
        elif op in (b"n", b"S", b"s"):
            self.pending = []
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            self._text(_shown(args).strip(), cm, tm)

    def _text(self, text: str, cm: list[float], tm: list[float]) -> None:
        if not text:
            return
        st = self.st
        scale = math.hypot(tm[0], tm[1]) * math.hypot(cm[0], cm[1])
        x, y = _apply(cm, tm[4], tm[5])
        over_dark = any(x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1
                        for x0, y0, x1, y1 in self.dark)
        if (st["render"] == 3 or st["size"] * scale < self.min_font_pt
                or (st["white"] and not over_dark)):
            self.hidden.append(text)


def hidden_pdf_text(path: str | Path, *, min_font_pt: float = 2.0) -> list[Finding]:
    """Text drawn so that a person looking at the page would not see it.

    White text is only hidden if it is not drawn over a dark filled shape:
    a table header in white on charcoal is ordinary typesetting, so filled
    rectangles are tracked and the text's position is tested against them.
    """
    from pypdf import PdfReader

    findings: list[Finding] = []
    for page in PdfReader(str(path)).pages:
        scan = _PageScan(min_font_pt)
        page.extract_text(visitor_operand_before=scan.visit)
        if scan.hidden:
            findings.append(Finding("hidden_text", _clip(" ".join(scan.hidden), 140)))
    return findings


def screen(path: str | Path, text: str, *, min_font_pt: float = 2.0) -> list[Finding]:
    """Everything R13 checks before a model is allowed to read the document."""
    found = screen_text(text)
    if str(path).lower().endswith(".pdf"):
        found = hidden_pdf_text(path, min_font_pt=min_font_pt) + found
    return found


# ---------------------------------------------------------------------------
# GSTIN: structure and check character, offline (R6)
# ---------------------------------------------------------------------------

_GSTIN = re.compile(r"^(?P<state>\d{2})(?P<pan>[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z])"
                    r"(?P<entity>[1-9A-Z])Z(?P<check>[0-9A-Z])$")
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# No leading word boundary: pypdf runs the letterhead name into the label
# ("...AlloysGSTIN 29AAB...").
GSTIN_IN_TEXT = re.compile(r"GSTIN\b\s*(?:no\.?|number)?\s*[:\-]?\s*([0-9]{2}[0-9A-Z]{13})\b",
                           re.IGNORECASE)


def gstin_check_char(first14: str) -> str:
    """The 15th character: a Luhn mod 36 over the first fourteen.

    Cross-checked against python-stdnum's in_.gstin in the test suite.
    """
    total = 0
    for i, c in enumerate(first14):
        v = _B36.index(c) * (2 if i % 2 else 1)
        total += v // 36 + v % 36
    return _B36[(36 - total % 36) % 36]


def gstin_problem(gstin: str) -> str:
    """'' if `gstin` is structurally valid, otherwise what is wrong with it."""
    g = (gstin or "").strip().upper()
    m = _GSTIN.match(g)
    if not m:
        return f"{gstin!r} is not shaped like a GSTIN"
    if not (1 <= int(m["state"]) <= 38 or m["state"] in ("97", "99")):
        return f"{gstin!r} has no valid state code"
    if gstin_check_char(g[:14]) != g[14]:
        return f"{gstin!r} fails its check character"
    return ""


# ---------------------------------------------------------------------------
# After the read: every value must be on the page
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\d.,])\d+(?:\.\d+)?")
_WORDS = re.compile(r"[a-z0-9]+")


def _numbers(text: str) -> set[Decimal]:
    return {dec(m.group(0)) for m in _NUMBER.finditer(text)}


def _tokens(text: str) -> list[str]:
    return _WORDS.findall(text.lower())


def ground(req: Any, text: str, *, dates_in_text: set[str]) -> list[str]:
    """Values in `req` that do not appear in `text`. Empty means fully grounded."""
    nums = _numbers(text)
    words = set(_tokens(text))
    flat = " ".join(_tokens(text))
    bad: list[str] = []

    if req.supplier and " ".join(_tokens(req.supplier)) not in flat:
        bad.append(f"supplier {req.supplier!r}")
    for i, line in enumerate(req.items, 1):
        toks = _tokens(line.description or "")
        if toks and sum(t in words for t in toks) / len(toks) < 0.8:
            bad.append(f"line {i} description {line.description!r}")
        if line.qty is not None and dec(line.qty) not in nums:
            bad.append(f"line {i} quantity {line.qty:g}")
        if line.rate is not None and dec(line.rate) not in nums:
            bad.append(f"line {i} rate {line.rate:g}")
    gst = getattr(req, "supplier_gstin", None)
    if gst and gst.upper() not in text.upper():
        bad.append(f"supplier GSTIN {gst!r}")
    req_by = getattr(req, "requester", None)
    if req_by and " ".join(_tokens(req_by)) not in flat:
        bad.append(f"requester {req_by!r}")
    if req.needed_by and req.needed_by not in dates_in_text:
        bad.append(f"needed-by date {req.needed_by}")
    if req.supplier_invoice_no and (normalise_invoice_no(req.supplier_invoice_no)
                                    not in normalise_invoice_no(text)):
        bad.append(f"supplier reference {req.supplier_invoice_no!r}")
    return bad

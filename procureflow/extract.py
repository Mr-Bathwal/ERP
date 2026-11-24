"""Unstructured in, `PurchaseRequest` out.

This is the only module that asks a model to read. Its job ends the moment
a validated `PurchaseRequest` exists; nothing here computes a total, applies
a tolerance or decides anything.

Three behaviours are load-bearing:

**Strict schema, one retry, then stop.** The model is given a JSON schema and
its output is validated with pydantic. On a validation failure it gets one
more attempt *with the validation error in the prompt*, which fixes the
common case — a date in the wrong format, a quantity as `"two"`. If the
second attempt also fails the run ends `needs_human` with the reason. It
never falls back to a partial parse, because a purchase order built from
three of four lines is worse than no purchase order.

**Resolution is separate from reading.** The model returns the item
description *as written*. Mapping that to an ERPNext item code happens in
`resolve_items`, against the live Item master, by exact match, then fuzzy
match, then — only if those fail and the margin is clear — a constrained
model choice from a shortlist. The model is never asked to produce an item
code from memory: a code that looks right and does not exist fails loudly,
but a code that looks right and belongs to a different item does not.

**No key is not a crash.** With no LLM configured, `extract()` runs a
deterministic rule-based parser good enough for the seeded email format, and
labels the result `method="heuristic"`. The eval reports the two separately,
so nobody mistakes a heuristic run for a model run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import PurchaseRequest, RequestLine, settings

SYSTEM = """You read purchase requests and supplier quotes for an Indian manufacturer.

Return ONLY what is written on the page. Specifically:

- Copy each item description verbatim. Do NOT invent or guess an item code.
- If a quantity is not stated, return null. Never assume 1.
- Dates must be yyyy-mm-dd. If a date is relative or ambiguous, return null.
- Rates are per unit, as printed, digits only (1240.00, not "Rs 1,240").
- Do not total anything. Do not compute tax. Do not judge whether a price is fair.

If something is illegible or absent, leave it null. A null field costs a
human thirty seconds. A guessed field costs the company a wrong payment."""


@dataclass
class ExtractResult:
    """What came back, and how."""

    request: PurchaseRequest | None
    method: str                       # llm | llm-retry | heuristic | failed
    attempts: int = 1
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    ms: int = 0
    raw: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.request is not None


# ---------------------------------------------------------------------------
# Reading the source
# ---------------------------------------------------------------------------

def read_source(path: str | Path) -> tuple[str, str]:
    """Return (text, kind) for an email or a PDF quote."""
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(p))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            return text, "pdf"
        except Exception as e:                                  # noqa: BLE001
            raise ValueError(f"could not read {p.name} as a PDF: {e}") from e
    return p.read_text(encoding="utf-8", errors="replace"), "email"


# ---------------------------------------------------------------------------
# The deterministic parser (no key, and the CI path)
# ---------------------------------------------------------------------------

_QTY_LINE = re.compile(
    r"^\s*[-*•]?\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>nos|no|pcs|pc|kg|kgs|box|boxes|units?|rolls?|pairs?|litres?|l)?\s*"
    r"(?:x|of|×)?\s*(?P<desc>[A-Za-z][^\n,;]{2,80}?)"
    r"(?:\s*(?:@|at)\s*(?:rs\.?|inr|₹)?\s*(?P<rate>[\d,]+(?:\.\d+)?))?\s*$",
    re.IGNORECASE | re.MULTILINE)

_DESC_FIRST = re.compile(
    r"^\s*[-*•]?\s*(?P<desc>[A-Za-z][^\n,;]{2,80}?)\s*[-–:,]\s*"
    r"(?:qty\.?\s*)?(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>nos|no|pcs|pc|kg|kgs|box|boxes|units?|pairs?)?"
    r"(?:\s*(?:@|at)\s*(?:rs\.?|inr|₹)?\s*(?P<rate>[\d,]+(?:\.\d+)?))?\s*$",
    re.IGNORECASE | re.MULTILINE)

# The lead-in, then as much of the line as could be a company name. The
# suffix is NOT matched here: a non-greedy match with the suffix inside the
# pattern stops at the first alternative that fits, so "Bharat Fasteners Pvt
# Ltd" came back as "Bharat Fasteners" because "Fasteners" is itself in the
# suffix list. The span is taken greedily and trimmed afterwards instead.
#   - `[^\n]` not `\s`: a greedy `\s` capture ran across the blank line and
#     swallowed the next paragraph, so "Vindhya Tooling Solutions Pvt Ltd"
#     came back as "order Please raise a PO on Vindhya Tooling Solutions...".
#   - `an?` not `a`: "raise **an** order on" is at least as common as "a".
# "...we need 20 nos ABS granules natural 25kg and also 12 nos PVC sheet..."
# Two items in one sentence is ordinary in a request email and invisible to
# a line-anchored pattern, so this one is not anchored.
_INLINE_QTY = re.compile(
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<uom>nos|no|pcs|pc|kgs?|box(?:es)?|units?|pairs?)\s+"
    r"(?P<desc>[A-Za-z][A-Za-z0-9 .\-/]{4,60}?)"
    r"(?=\s*(?:,|;|\.|\band\b|\balso\b|\bby\b|$))"
    r"(?:\s*@\s*(?:rs\.?|inr|₹)?\s*(?P<rate>[\d,]+(?:\.\d+)?))?",
    re.IGNORECASE)

# Labels that look like "description: number" to a line parser. Without this
# stoplist, "Date: 2026-11-20" became an item with a quantity of 2026.
_NOT_AN_ITEM = re.compile(
    r"^(date|valid\s*until|quote\s*(no|number)?|invoice\s*(no|number)?|ref|"
    r"delivery\s*required(\s*by)?|needed\s*by|required\s*by|deliver\s*by|"
    r"gstin|gst|subtotal|sub\s*total|total|amount|rate|qty|quantity|uom|"
    r"description|terms|to|from|supplier|vendor|page|tel|phone|email|"
    r"po|purchase\s*order)\b",
    re.IGNORECASE)

_UOMS = {"nos", "no", "pcs", "pc", "kg", "kgs", "box", "boxes", "unit",
         "units", "pair", "pairs", "litre", "litres", "l", "set", "sets"}

_SUPPLIER = re.compile(
    r"(?:supplier|vendor|quote\s+from|"
    r"raise\s+(?:an?\s+)?(?:po|order|purchase\s+order)\s+(?:on|to|with|from)|"
    r"order\s+(?:from|on)|from)"
    r"[ \t]*[:\-]?[ \t]*(?P<name>[A-Z][A-Za-z&.\- ]{2,70})",
    re.IGNORECASE)

# A supplier often sits on a line of its own with no lead-in at all, which is
# how `req-expedite` is written and how most forwarded quotes arrive.
_SUPPLIER_BARE = re.compile(r"^[ \t]*(?P<name>[A-Z][A-Za-z&.\- ]{4,70})[ \t]*$",
                            re.MULTILINE)

# Ordered longest-first so "Private Limited" wins over "Limited".
_SUFFIXES = ("Private Limited", "Pvt. Ltd.", "Pvt Ltd.", "Pvt. Ltd", "Pvt Ltd",
             "Limited", "LLP", "Ltd.", "Ltd", "Alloys", "Polymers", "Abrasives",
             "Electricals", "Lubricants", "Fasteners", "Industries", "Enterprises")


# "R. Iyer", "p. deshmukh", "A. K. Menon" on a line of their own: the sign-off.
# The last one in the message is the requester -- R12's segregation-of-duties
# check compares it with the approver, so it must not be guessed from a
# name mentioned in passing ("raise an order on Deccan ...").
_SIGNOFF = re.compile(
    r"^[ \t]*(?P<name>[A-Z]\.(?:[ \t]?[A-Z]\.)*[ \t]?[A-Z][A-Za-z'\-]{1,30})[ \t]*$",
    re.MULTILINE | re.IGNORECASE)


def _requester(text: str) -> str | None:
    found = list(_SIGNOFF.finditer(text))
    return found[-1]["name"].strip() if found else None


def _trim_supplier(span: str) -> str | None:
    """Cut a candidate span at the end of the longest company suffix in it.

    Greedy capture overshoots into the rest of the sentence, so the span is
    trimmed to the *last* suffix that appears -- which is what makes
    "Bharat Fasteners Pvt Ltd for the following" resolve to the full
    registered name rather than to its first two words.
    """
    text = " ".join(span.split())
    best = None
    for suf in _SUFFIXES:
        idx = text.lower().rfind(suf.lower())
        if idx == -1:
            continue
        end = idx + len(suf)
        if best is None or end > best:
            best = end
    if best is None:
        return None
    return text[:best].strip(" .,:;-")

_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DATE_DMY = re.compile(r"\b(\d{1,2})[/\-\s](\d{1,2}|[A-Za-z]{3,9})[/\-\s](20\d{2})\b")
_INV_NO = re.compile(r"(?:invoice|quote|quotation|bill|ref)\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-]{3,20})",
                     re.IGNORECASE)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_date(text: str) -> str | None:
    if (m := _DATE_ISO.search(text)):
        return m.group(0)
    if (m := _DATE_DMY.search(text)):
        d, mon, y = m.groups()
        if mon.isdigit():
            mi = int(mon)
        else:
            mi = _MONTHS.get(mon[:3].lower(), 0)
        if mi:
            try:
                return date(int(y), mi, int(d)).isoformat()
            except ValueError:
                return None
    return None


def _num(cell: str) -> float | None:
    cell = cell.strip().replace(",", "")
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def _pdf_table_items(text: str) -> list[RequestLine]:
    """Walk a quote table that pypdf flattened into one cell per line.

    The layout engine writes cells top-to-bottom, so a three-column row
    arrives as three consecutive lines and no regex over a single line can
    see a row. The header is located, then cells are consumed in groups of
    six (`#`, description, qty, uom, rate, amount) for as long as the first
    cell of a group is a small integer. The totals block breaks that shape,
    which is exactly how the walk knows the table has ended.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.lower() == "description")
    except StopIteration:
        return []

    # Step past the remaining header cells to the first data cell.
    i = start + 1
    while i < len(lines) and lines[i].lower() in {
            "qty", "uom", "rate", "amount", "rate (inr)", "amount (inr)",
            "unit", "price", "#", ""}:
        i += 1

    out: list[RequestLine] = []
    while i + 5 < len(lines):
        idx, desc, qty, uom, rate = lines[i:i + 5]
        if _num(idx) is None or not float(_num(idx) or 0).is_integer():
            break
        q, r = _num(qty), _num(rate)
        if not desc or q is None or r is None or uom.lower() not in _UOMS:
            break
        if _NOT_AN_ITEM.match(desc):
            break
        out.append(RequestLine(description=desc, qty=q, uom=uom, rate=r))
        i += 6                      # the sixth cell is the line amount
    return out


def _pdf_supplier(text: str) -> str | None:
    """The letterhead name, which reportlab runs into the GSTIN with no space.

    Line 0 of these quotes comes out as
    `Deccan Steel & AlloysGSTIN 29AABCD1234E1Z5`, so the name is cut at the
    first marker that cannot be part of it before the suffix trim runs.
    """
    for raw in text.splitlines()[:6]:
        head = re.split(r"GSTIN|\bTel\b|\bPhone\b|\bEmail\b|\d{2}[A-Z]{5}", raw)[0]
        if (name := _trim_supplier(head)):
            return name
    return None


def heuristic_extract(text: str, *, source_file: str = "", kind: str = "email") -> PurchaseRequest:
    """A parser with no model in it.

    Covers the shape the seeded emails and quotes are written in. It exists
    so the test suite and CI never need a key, and so a missing credential
    degrades the system rather than stopping it. It is not claimed to
    generalise, and the eval reports heuristic runs separately.
    """
    req = PurchaseRequest(source_file=source_file, source_kind=kind)  # type: ignore[arg-type]

    if kind == "pdf":
        req.supplier = _pdf_supplier(text)
    if not req.supplier:
        for rx in (_SUPPLIER, _SUPPLIER_BARE):
            for m in rx.finditer(text):
                if (name := _trim_supplier(m.group("name"))):
                    req.supplier = name
                    break
            if req.supplier:
                break

    needed = None
    for line in text.splitlines():
        if re.search(r"need(ed)?\s+(it\s+)?by|required\s+by|deliver\s+by|by\b", line, re.I):
            needed = _parse_date(line)
            if needed:
                break
    req.needed_by = needed or _parse_date(text)

    if (m := _INV_NO.search(text)):
        req.supplier_invoice_no = m.group(1).strip()

    if kind != "pdf":
        req.requester = _requester(text)

    # A PDF table is emitted by pypdf one *cell* per line, so it is walked
    # as a cell sequence rather than matched as rows. Where that succeeds it
    # is authoritative and the loose line patterns are not run at all --
    # they would only re-add the header and total cells as phantom items.
    if kind == "pdf":
        rows = _pdf_table_items(text)
        if rows:
            req.items = rows
            return req

    seen: set[str] = set()
    for rx in (_QTY_LINE, _DESC_FIRST, _INLINE_QTY):
        for m in rx.finditer(text):
            desc = " ".join(m.group("desc").split()).strip(" .,:-")
            if not desc or len(desc) < 3:
                continue
            low = desc.lower()
            if low in seen:
                continue
            if re.match(r"^(need|please|we|hi|hello|thanks|regards|dear|the|and|for|our)\b", low):
                continue
            if _NOT_AN_ITEM.match(low):
                continue
            seen.add(low)
            rate = m.groupdict().get("rate")
            req.items.append(RequestLine(
                description=desc,
                qty=float(m.group("qty")) if m.group("qty") else None,
                uom=(m.group("uom") or None),
                rate=float(str(rate).replace(",", "")) if rate else None,
            ))
    return req


# ---------------------------------------------------------------------------
# The model path
# ---------------------------------------------------------------------------

def _schema() -> dict[str, Any]:
    """The JSON schema handed to the model, trimmed of pydantic internals."""
    s = PurchaseRequest.model_json_schema()
    for drop in ("source_file", "source_kind"):
        s.get("properties", {}).pop(drop, None)
    props = s.get("properties", {}).get("items", {})
    defs = s.get("$defs", {}).get("RequestLine", {})
    for drop in ("item_code", "match_method", "match_score"):
        defs.get("properties", {}).pop(drop, None)
    _ = props
    return s


def _client() -> Any:
    from openai import OpenAI
    return OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                  max_retries=2, timeout=60.0)


def llm_extract(text: str, *, source_file: str = "", kind: str = "email",
                model: Any = None) -> ExtractResult:
    """Ask the model for structured JSON; validate; retry once with the error.

    `model` is an injection point for tests — any callable taking
    `(messages)` and returning `(content, in_tokens, out_tokens)`.
    """
    import time
    t0 = time.time()
    schema = json.dumps(_schema(), separators=(",", ":"))
    user = (f"Source: {source_file or 'inline'} ({kind})\n\n"
            f"Document:\n'''\n{text[:12000]}\n'''\n\n"
            f"Return JSON matching this schema exactly:\n{schema}")

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]

    def call(msgs: list[dict[str, str]]) -> tuple[str, int, int]:
        if model is not None:
            return model(msgs)
        resp = _client().chat.completions.create(
            model=settings.llm_model, messages=msgs, temperature=0,
            response_format={"type": "json_object"})
        u = resp.usage
        return (resp.choices[0].message.content or "",
                getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))

    tin = tout = 0
    raw = ""
    last_error = ""
    for attempt in (1, 2):
        try:
            raw, a, b = call(messages)
            tin += a
            tout += b
        except Exception as e:                                  # noqa: BLE001
            return ExtractResult(None, "failed", attempt, f"{type(e).__name__}: {e}",
                                 tin, tout, int((time.time() - t0) * 1000), raw)
        try:
            payload = json.loads(_strip_fences(raw))
            req = PurchaseRequest.model_validate(
                {**payload, "source_file": source_file, "source_kind": kind})
            return ExtractResult(req, "llm" if attempt == 1 else "llm-retry", attempt,
                                 "", tin, tout, int((time.time() - t0) * 1000), raw)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)[:600]
            if attempt == 2:
                break
            # The retry is the whole trick: hand the model its own error.
            messages = messages + [
                {"role": "assistant", "content": raw[:2000]},
                {"role": "user", "content":
                 f"That did not validate:\n{last_error}\n\n"
                 "Return corrected JSON only. Remember: unknown values are null, "
                 "dates are yyyy-mm-dd, quantities are numbers."},
            ]

    return ExtractResult(None, "failed", 2, last_error, tin, tout,
                         int((time.time() - t0) * 1000), raw)


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def extract(path: str | Path, *, model: Any = None,
            force_heuristic: bool = False) -> ExtractResult:
    """Read a file and produce a validated `PurchaseRequest`, or say why not."""
    text, kind = read_source(path)
    name = Path(path).name

    if force_heuristic or (model is None and not settings.llm_configured):
        req = heuristic_extract(text, source_file=name, kind=kind)
        notes = [] if req.is_complete else req.missing()
        return ExtractResult(req, "heuristic", 1, "", notes=notes)

    res = llm_extract(text, source_file=name, kind=kind, model=model)
    if res.request is not None and not res.request.is_complete:
        res.notes = res.request.missing()
    return res


# ---------------------------------------------------------------------------
# Resolution: free text to catalogue codes
# ---------------------------------------------------------------------------

@dataclass
class Catalogue:
    """The Item master, indexed for matching."""

    codes: list[str]
    names: dict[str, str]

    @classmethod
    def from_erp(cls, erp: Any, limit: int = 500) -> Catalogue:
        rows = erp.list_docs("Item", fields=["name", "item_name", "description"], limit=limit)
        codes = [str(r["name"]) for r in rows]
        names = {str(r["name"]): str(r.get("item_name") or r.get("description") or r["name"])
                 for r in rows}
        return cls(codes, names)


def resolve_items(req: PurchaseRequest, cat: Catalogue, *,
                  fuzzy_floor: int = 72, margin: int = 8) -> list[str]:
    """Attach an `item_code` to each line. Returns notes about how.

    Three tiers, in order:

    1. **Exact** on the code, case-insensitively. Free and unambiguous.
    2. **Fuzzy** on the item name with rapidfuzz. Accepted only if the best
       score clears `fuzzy_floor` *and* beats the runner-up by `margin`. The
       margin is the important half: a request for "hex bolt" against a
       catalogue holding M8 and M10 hex bolts scores both at 90, and picking
       the first is a coin flip dressed as a match.
    3. **Unresolved**, which escalates. There is deliberately no fourth tier
       that guesses.
    """
    notes: list[str] = []
    try:
        from rapidfuzz import fuzz, process
        have_rf = True
    except Exception:                                           # noqa: BLE001
        have_rf = False

    upper = {c.upper(): c for c in cat.codes}

    for line in req.items:
        probe = (line.description or "").strip()
        if not probe:
            continue

        if (hit := upper.get(probe.upper())):
            line.item_code, line.match_method, line.match_score = hit, "exact", 100.0
            continue

        if not have_rf:
            lowered = probe.lower()
            cands = [c for c in cat.codes if lowered in cat.names[c].lower()]
            if len(cands) == 1:
                line.item_code, line.match_method, line.match_score = cands[0], "substring", 80.0
            else:
                notes.append(f"{probe!r}: no unambiguous match without rapidfuzz")
            continue

        choices = {c: cat.names[c] for c in cat.codes}
        ranked = process.extract(probe, choices, scorer=fuzz.WRatio, limit=3)
        if not ranked:
            notes.append(f"{probe!r}: nothing in the catalogue resembles this")
            continue

        best_name, best_score, best_code = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0
        if best_score >= fuzzy_floor and (best_score - runner) >= margin:
            line.item_code = best_code
            line.match_method = "fuzzy"
            line.match_score = float(best_score)
        else:
            why = ("below the match floor" if best_score < fuzzy_floor
                   else f"too close to call against {ranked[1][2]} "
                        f"({best_score:.0f} vs {runner:.0f})")
            notes.append(f"{probe!r}: {why}; best guess was {best_code} ({best_name})")
    return notes

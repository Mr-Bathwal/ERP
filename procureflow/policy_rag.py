"""Policy retrieval: embeddings over the written procurement policy.

The agent does not know any thresholds. It retrieves them from
`policy/procurement_policy.md`, and every decision it records carries the
rule id it acted on. Change ₹50,000 to ₹75,000 in the document and the next
run behaves differently, with no code change and no redeploy — which is the
property an implementation consultant is actually selling.

**Chunking is by rule, not by token window.** The obvious thing is to split
on 500 characters with an overlap; here that is wrong. A rule is a semantic
unit — threshold, effect, and the reason it is that number — and splitting
it mid-paragraph produces a chunk that states a limit without its scope, or
a rationale with no limit attached. Retrieval then returns half a rule and
the agent cites R3 while quoting the sentence that explains R4. Rules are
short enough to embed whole, so they are.

**Retrieval is a floor, not the decision.** `evaluate()` reads the thresholds
out of the retrieved rules by parsing the document's own table, and the
numbers that drive arithmetic come from there. A rule that is not retrieved
cannot be cited, but R5/R6/R8/R10 are additionally hard-coded as always
applicable: a vector search that happens to rank them low must not be able
to disable the controls whose entire purpose is that they cannot be skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approval import Approver, parse_authority

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "procurement_policy.md"

# Controls that apply whether or not the vector store surfaces them. See the
# module docstring: retrieval must not be able to switch off an authority rule.
ALWAYS_APPLICABLE = ("R5", "R6", "R8", "R10", "R12", "R13")


@dataclass
class Rule:
    """One numbered rule, whole."""

    rule_id: str
    title: str
    text: str
    threshold: str = ""

    def cite(self) -> str:
        return f"{self.rule_id} — {self.title}"

    def as_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "title": self.title,
                "threshold": self.threshold, "text": self.text}


def parse_policy(path: Path | None = None) -> list[Rule]:
    """Split the policy into rules on its `## R<n> — <title>` headings.

    Parsing the document the humans edit, rather than keeping a parallel
    machine-readable copy, is deliberate: two copies of a threshold is one
    copy too many, and the one that drifts is always the one nobody reads.
    """
    text = (path or POLICY_PATH).read_text(encoding="utf-8")

    # The summary table is a restatement, not a rule; exclude it so it is
    # not retrieved as a tenth chunk that answers every query equally badly.
    body = text.split("## Rule summary")[0]

    rules: list[Rule] = []
    pattern = re.compile(r"^##\s+(R\d+)\s+[—-]\s+(.+?)\s*$", re.MULTILINE)
    marks = list(pattern.finditer(body))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        chunk = body[start:end].strip()
        rules.append(Rule(rule_id=m.group(1), title=m.group(2).strip(), text=chunk))

    # The summary table is the one place every threshold is stated in a
    # uniform shape, so it is used to annotate the rules rather than ignored.
    for row in re.finditer(r"^\|\s*(R\d+)\s*\|([^|]*)\|([^|]*)\|", text, re.MULTILINE):
        rid, _subject, threshold = row.group(1), row.group(2), row.group(3)
        for r in rules:
            if r.rule_id == rid:
                r.threshold = threshold.strip()
    return rules


def policy_version(path: Path | None = None) -> str:
    """The `**Version x.y**` the policy owner stamped on the document."""
    text = (path or POLICY_PATH).read_text(encoding="utf-8")
    m = re.search(r"\*\*Version\s+([\d.]+)\*\*", text)
    return m.group(1) if m else "unversioned"


def fingerprint(rules: list[Rule]) -> str:
    """SHA-256 of the rules the engine actually runs on.

    Hashed from the parsed rules rather than the file bytes, so a change to
    whitespace or line endings is not a policy change but any change to a
    rule's text, title or threshold is.
    """
    import hashlib
    blob = "\n".join(r.rule_id + "|" + r.title + "|" + r.threshold + "|" + " ".join(r.text.split())
                     for r in rules)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PolicyError(RuntimeError):
    """The policy did not parse into the numbers the engine needs. Fail closed."""


class PolicyIndex:
    """Vector index over the policy, with a deterministic fallback.

    ChromaDB with its bundled MiniLM is the default, so no embedding key is
    needed. If Chroma is unavailable — an environment without the ONNX
    runtime, or CI where downloading a model is unwelcome — this falls back
    to lexical scoring over the same chunks. The fallback is not a toy: it
    keeps retrieval deterministic in tests, and the tests assert which rules
    come back, so a silent switch between the two cannot go unnoticed.
    """

    def __init__(self, rules: list[Rule] | None = None, *, prefer_vectors: bool = True) -> None:
        self.version = policy_version() if rules is None else "custom"
        self.rules = rules if rules is not None else parse_policy()
        self.sha256 = fingerprint(self.rules)
        self._by_id = {r.rule_id: r for r in self.rules}
        self.backend = "lexical"
        self._collection = None
        if prefer_vectors:
            self._try_chroma()

    def _try_chroma(self) -> None:
        try:
            import chromadb
            client = chromadb.EphemeralClient()
            col = client.get_or_create_collection("procurement_policy")
            col.add(
                ids=[r.rule_id for r in self.rules],
                documents=[f"{r.rule_id} {r.title}\n{r.text}" for r in self.rules],
                metadatas=[{"rule_id": r.rule_id, "title": r.title} for r in self.rules],
            )
            self._collection = col
            self.backend = "chromadb"
        except Exception:                                       # noqa: BLE001
            self._collection = None
            self.backend = "lexical"

    def _lexical(self, query: str, k: int) -> list[Rule]:
        """Token-overlap scoring, weighted toward the title.

        Crude on purpose. It exists to be predictable, not clever.
        """
        q = set(re.findall(r"[a-z0-9]+", query.lower()))
        scored: list[tuple[float, Rule]] = []
        for r in self.rules:
            title = set(re.findall(r"[a-z0-9]+", r.title.lower()))
            body = set(re.findall(r"[a-z0-9]+", r.text.lower()))
            score = len(q & title) * 3 + len(q & body)
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], x[1].rule_id))
        return [r for _, r in scored[:k]]

    def retrieve(self, query: str, k: int = 4) -> list[Rule]:
        """Rules relevant to `query`, best first."""
        if self._collection is not None:
            try:
                res = self._collection.query(query_texts=[query], n_results=min(k, len(self.rules)))
                ids = (res.get("ids") or [[]])[0]
                return [self._by_id[i] for i in ids if i in self._by_id]
            except Exception:                                   # noqa: BLE001
                pass
        return self._lexical(query, k)

    def get(self, rule_id: str) -> Rule | None:
        return self._by_id.get(rule_id)

    def applicable(self, query: str, k: int = 4) -> list[Rule]:
        """Retrieved rules, plus the controls that always apply.

        Order is retrieval order first, then the always-on controls, so a
        citation list reads as "what this request raised" followed by "what
        applies regardless".
        """
        found = self.retrieve(query, k)
        seen = {r.rule_id for r in found}
        for rid in ALWAYS_APPLICABLE:
            r = self._by_id.get(rid)
            if r and rid not in seen:
                found.append(r)
        return found


# ---------------------------------------------------------------------------
# Thresholds, read out of the policy rather than hard-coded
# ---------------------------------------------------------------------------

@dataclass
class PolicyNumbers:
    """The thresholds, parsed from the document the humans maintain."""

    approval_ceiling_paise: int = 50_000 * 100
    price_pct: float = 5.0
    price_abs_paise: int = 500 * 100
    qty_pct: float = 2.0
    qty_abs_units: float = 1.0
    expedite_days: int = 2
    run_ceiling_paise: int = 5_00_000 * 100
    preferred: tuple[str, ...] = field(default_factory=tuple)
    approvers: tuple[Approver, ...] = field(default_factory=tuple)
    hidden_font_pt: float = 2.0
    split_window_days: int = 7
    duplicate_window_days: int = 30
    source: str = "policy/procurement_policy.md"


def _rupees_to_paise(text: str) -> int | None:
    """₹50,000 / ₹5,00,000 → paise. Indian grouping, so no locale parsing."""
    m = re.search(r"₹\s*([\d,]+)", text)
    if not m:
        return None
    return int(m.group(1).replace(",", "")) * 100


def read_numbers(rules: list[Rule] | None = None) -> PolicyNumbers:
    """Pull every threshold the engine needs out of the policy text.

    Each value falls back to the documented default if the sentence it lives
    in has been reworded. A reworded policy that silently reverts a
    threshold would be the worst possible failure here, so
    `verify_numbers()` exists to assert the parse actually found them, and a
    test calls it against the shipped document.
    """
    rules = rules if rules is not None else parse_policy()
    by_id = {r.rule_id: r for r in rules}
    n = PolicyNumbers()

    if (r1 := by_id.get("R1")):
        if (v := _rupees_to_paise(r1.threshold or r1.text)) is not None:
            n.approval_ceiling_paise = v

    if (r3 := by_id.get("R3")):
        if (m := re.search(r"(\d+(?:\.\d+)?)\s*%", r3.text)):
            n.price_pct = float(m.group(1))
        if (v := _rupees_to_paise(r3.text)) is not None:
            n.price_abs_paise = v

    if (r4 := by_id.get("R4")):
        if (m := re.search(r"(\d+(?:\.\d+)?)\s*%", r4.text)):
            n.qty_pct = float(m.group(1))
        if (m := re.search(r"\*\*(\d+(?:\.\d+)?)\s*unit", r4.text)):
            n.qty_abs_units = float(m.group(1))

    if (r7 := by_id.get("R7")):
        if (m := re.search(r"\*\*(\d+)\s*calendar days?\*\*", r7.text)):
            n.expedite_days = int(m.group(1))

    if (r10 := by_id.get("R10")):
        if (v := _rupees_to_paise(r10.text)) is not None:
            n.run_ceiling_paise = v

    if (r2 := by_id.get("R2")):
        n.preferred = _preferred_from(r2.text)

    if (r12 := by_id.get("R12")):
        n.approvers = parse_authority(r12.text)

    if (r13 := by_id.get("R13")):
        if (m := re.search(r"\*\*(\d+(?:\.\d+)?)\s*pt\*\*", r13.text)):
            n.hidden_font_pt = float(m.group(1))

    if (r15 := by_id.get("R15")):
        if (m := re.search(r"\*\*(\d+)\s*days?\*\*", r15.text)):
            n.split_window_days = int(m.group(1))

    if (r5 := by_id.get("R5")):
        if (m := re.search(r"\*\*(\d+)\s*days?\*\*", r5.text)):
            n.duplicate_window_days = int(m.group(1))

    return n


# Bold is used in R2 for emphasis as well as for supplier names ("does **not**
# substitute silently", "> **Why.**"), and a name can wrap across a line. So
# candidates are whitespace-collapsed and then filtered to things shaped like a
# registered company, rather than taking every bold span.
_COMPANY_SUFFIX = re.compile(
    r"\b(Ltd|Limited|LLP|Pvt|Private|Inc|GmbH|Co|Company|Alloys|Polymers|"
    r"Abrasives|Electricals|Lubricants|Fasteners|Industries|Enterprises)\b")


def _preferred_from(text: str) -> tuple[str, ...]:
    """Supplier names from R2, ignoring bold used for emphasis."""
    out: list[str] = []
    for raw in re.findall(r"\*\*([^*]+?)\*\*", text, re.DOTALL):
        name = " ".join(raw.split())           # collapse the line wrap
        if len(name.split()) < 2:
            continue                            # "not"
        if not name[:1].isupper():
            continue
        if not _COMPANY_SUFFIX.search(name):
            continue                            # "Why this number."
        if name not in out:
            out.append(name)
    return tuple(out)


def verify_numbers(n: PolicyNumbers) -> list[str]:
    """Sanity-check a parse. Returns complaints; empty means the parse held.

    A policy document is edited by people who are not thinking about the
    regular expressions that read it. This turns a silent reversion to
    defaults into a loud failure at import time.
    """
    bad: list[str] = []
    if n.approval_ceiling_paise <= 0:
        bad.append("R1 approval ceiling did not parse")
    if not (0 < n.price_pct <= 100):
        bad.append("R3 price percentage out of range")
    if n.price_abs_paise <= 0:
        bad.append("R3 cash cap did not parse")
    if not (0 < n.qty_pct <= 100):
        bad.append("R4 quantity percentage out of range")
    if n.run_ceiling_paise <= n.approval_ceiling_paise:
        bad.append("R10 run ceiling must exceed the R1 approval ceiling")
    if len(n.preferred) < 2:
        bad.append("R2 preferred suppliers did not parse")
    # A count alone passed while the parse was returning 'not' and 'Why.'
    # alongside the real names, so the shape is checked too.
    for name in n.preferred:
        if "\n" in name or len(name.split()) < 2 or not _COMPANY_SUFFIX.search(name):
            bad.append(f"R2 preferred supplier {name!r} does not look like a company name")
    if not n.approvers:
        bad.append("R12 delegation-of-authority table did not parse")
    elif max(a.limit_paise for a in n.approvers) < n.run_ceiling_paise:
        # Otherwise an order R10 allows could need an approval nobody can give.
        bad.append("R12: no approver's authority reaches the R10 run ceiling")
    return bad

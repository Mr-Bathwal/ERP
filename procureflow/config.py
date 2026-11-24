"""Configuration and the typed request model.

Two things live here because everything else imports both: the settings
object, and `PurchaseRequest` — the schema the extractor must produce and the
graph consumes.

`PurchaseRequest` is the contract between the probabilistic half of the
system and the deterministic half. Everything upstream of it is a model
reading a document; everything downstream is arithmetic against ERPNext. The
model is asked for **strings and numbers it can see on the page** and nothing
else: no totals, no tax, no judgement about whether a price is reasonable. If
a field is not legible it is left empty and the run escalates, because a
plausible guess in a purchase order is indistinguishable from a fact until
the money has moved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

try:                                                            # optional
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                               # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """Everything environment-dependent, read once.

    The LLM is reached through an OpenAI-compatible client so the provider is
    a base URL and a model name rather than a code path. Groq's free tier is
    the default because it serves tool-calling Llama 3.3 70B at no cost,
    which matters for an evaluation harness that runs twenty scenarios
    repeatedly.
    """

    erp_url: str = os.getenv("ERPNEXT_URL", "http://localhost:8080")
    erp_key: str = os.getenv("ERPNEXT_API_KEY", "")
    erp_secret: str = os.getenv("ERPNEXT_API_SECRET", "")

    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    llm_model: str = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")

    company: str = os.getenv("PF_COMPANY", "ProcureFlow Demo Pvt Ltd")
    abbr: str = os.getenv("PF_ABBR", "PFD")
    warehouse: str = os.getenv("PF_WAREHOUSE", "Stores - PFD")
    currency: str = "INR"
    price_list: str = os.getenv("PF_PRICE_LIST", "Standard Buying")   # R14

    # R10. Held here as well as in the policy because it is a circuit
    # breaker: if policy retrieval itself fails, this still applies.
    run_ceiling_paise: int = int(os.getenv("PF_RUN_CEILING", "50000000"))

    trace_db: str = os.getenv("PF_TRACE_DB", "eval/results/traces.sqlite")

    @property
    def erp_configured(self) -> bool:
        return bool(self.erp_key and self.erp_secret)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    def redacted(self) -> dict[str, str]:
        """Settings safe to write into a trace row or a log line."""
        return {
            "erp_url": self.erp_url,
            "erp_configured": str(self.erp_configured),
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_configured": str(self.llm_configured),
            "company": self.company,
        }


settings = Settings()


# ---------------------------------------------------------------------------
# The extraction contract
# ---------------------------------------------------------------------------

class RequestLine(BaseModel):
    """One line a requester asked for, as written.

    `item_code` is left empty by the extractor. Mapping free text to a
    catalogue code is resolution, not reading, and it happens in
    `extract.py::resolve_items` against the live Item master — a model
    inventing `FAS-M8-HEX` because it looks like other codes it has seen is
    the exact failure this separation prevents.
    """

    description: str = Field(description="the item as the requester wrote it, verbatim")
    qty: float | None = Field(default=None, description="quantity, or null if not stated")
    uom: str | None = Field(default=None, description="unit as written: nos, kg, box, pcs")
    rate: float | None = Field(default=None, description="unit price if the document states one")

    item_code: str | None = None          # filled by resolution, never by the model
    match_method: str | None = None       # exact | fuzzy | llm | unresolved
    match_score: float | None = None

    @field_validator("qty", "rate")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("must not be negative")
        return v


class PurchaseRequest(BaseModel):
    """A request, structured. The boundary between reading and deciding."""

    supplier: str | None = Field(default=None, description="supplier name as written, or null")
    items: list[RequestLine] = Field(default_factory=list)
    needed_by: str | None = Field(default=None, description="ISO date yyyy-mm-dd, or null")
    requester: str | None = Field(default=None, description="who asked, if stated")
    supplier_invoice_no: str | None = Field(
        default=None, description="supplier's own invoice/quote number, if stated")
    notes: str | None = None

    source_file: str = ""
    source_kind: Literal["email", "pdf", "unknown"] = "unknown"

    @field_validator("needed_by")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        """Accept only an ISO date, so a downstream date comparison is safe.

        The model is told to emit yyyy-mm-dd. A value it cannot normalise
        becomes None rather than a guess: "next Tuesday" resolved by a model
        with no calendar is a date nobody chose.
        """
        if not v:
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            return None

    # -- derived -----------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """Enough to act on: a supplier, at least one line, every line with a quantity."""
        return bool(self.supplier) and bool(self.items) and all(
            i.qty is not None and i.qty > 0 for i in self.items)

    def missing(self) -> list[str]:
        """Why `is_complete` is False, in words a buyer can act on."""
        out: list[str] = []
        if not self.supplier:
            out.append("no supplier named")
        if not self.items:
            out.append("no items found")
        for i in self.items:
            if i.qty is None or i.qty <= 0:
                out.append(f"no quantity for {i.description!r}")
        return out

    def needs_expedite(self, today: date | None = None) -> bool:
        """R7: needed fewer than two calendar days out."""
        if not self.needed_by:
            return False
        today = today or date.today()
        try:
            want = datetime.strptime(self.needed_by, "%Y-%m-%d").date()
        except ValueError:
            return False
        return (want - today) < timedelta(days=2)

    def unresolved(self) -> list[RequestLine]:
        return [i for i in self.items if not i.item_code]


# ---------------------------------------------------------------------------
# Run outcomes
# ---------------------------------------------------------------------------

class RunState(str, Enum):
    """Where a run ended. Every one of these is a designed stopping place.

    `NEEDS_HUMAN` and `ESCALATED` are outcomes, not failures — the policy
    asked for a person and got one. Only `FAILED` means the system did
    something it did not intend.
    """

    RUNNING = "running"                  # in flight; never a final answer
    COMPLETED = "completed"              # ran to the end, match clean
    COMPLETED_WITH_EXCEPTIONS = "completed_with_exceptions"
    AWAITING_APPROVAL = "awaiting_approval"
    NEEDS_HUMAN = "needs_human"          # extraction incomplete, R8
    ESCALATED = "escalated"              # policy stopped it, e.g. R6
    BLOCKED = "blocked"                  # R5 duplicate, R10 ceiling
    FAILED = "failed"                    # unhandled: a defect, not a decision
    DRY_RUN = "dry_run"


TERMINAL = {RunState.COMPLETED, RunState.COMPLETED_WITH_EXCEPTIONS,
            RunState.NEEDS_HUMAN, RunState.ESCALATED, RunState.BLOCKED,
            RunState.FAILED, RunState.DRY_RUN}


@dataclass
class RunContext:
    """Per-run switches that must not be global state.

    `dry_run` is carried here and enforced at the tool boundary rather than
    checked at each call site, so a new node cannot forget it.
    """

    run_id: str
    dry_run: bool = False
    receipt_mode: Literal["api", "computer_use"] = "api"
    approver: str = "buyer"
    today: date = field(default_factory=date.today)

"""Goods receipt driven through the ERPNext web UI, then verified by API.

Placeholder wired into the graph so `receipt_mode="computer_use"` resolves.
Replaced by the browser-use implementation in Phase 5; until then it raises
rather than silently falling back to the API, because a mode that quietly
does something else is worse than one that is absent.
"""

from __future__ import annotations

from typing import Any

from .tools import ToolCall, ToolRefused


def receive_via_ui(erp: Any, ctx: Any, *, po: Any,
                   received: dict[str, float] | None = None) -> tuple[Any, ToolCall]:
    raise ToolRefused(
        "computer-use mode is not enabled in this build; run with mode=api")

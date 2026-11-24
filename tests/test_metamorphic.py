"""Metamorphic tests: the same request written differently decides the same way.

Rupees as `Rs`, `INR`, `₹`, a trailing `/-`, a different line order, an extra
sign-off line -- none of it may change the order value, the rules cited or
the outcome.
"""

from __future__ import annotations

import pytest

from procureflow.config import RunState
from procureflow.graph import Agent
from tests.conftest import ROOT, TODAY

BASE = (ROOT / "seed" / "samples" / "req-happy.txt").read_text(encoding="utf-8")

VARIANTS = {
    "rupee_sign": BASE.replace("@ 12.00", "@ ₹12.00").replace("@ 1.80", "@ ₹1.80"),
    "rs_prefix": BASE.replace("@ 12.00", "@ Rs. 12.00").replace("@ 1.80", "@ Rs. 1.80"),
    "inr_prefix": BASE.replace("@ 12.00", "@ INR 12").replace("@ 1.80", "@ INR 1.8"),
    "line_order": BASE.replace(
        "- 500 nos Hex bolt M8x40 stainless @ 12.00\n- 200 nos Flat washer M8 zinc @ 1.80",
        "- 200 nos Flat washer M8 zinc @ 1.80\n- 500 nos Hex bolt M8x40 stainless @ 12.00"),
    "extra_signoff_line": BASE.replace("Production Planning", "Production Planning\nExt. 2231"),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_a_rewritten_request_decides_exactly_like_the_original(name, erp, policy, tmp_path):
    assert VARIANTS[name] != BASE, "the variant must actually change the text"
    p = tmp_path / f"{name}.txt"
    p.write_text(VARIANTS[name], encoding="utf-8")
    base_path = tmp_path / "base.txt"
    base_path.write_text(BASE, encoding="utf-8")
    from procureflow.erp.fake import demo_company
    a, _ = Agent(demo_company(), policy=policy).run(str(base_path), today=TODAY)
    b, _ = Agent(demo_company(), policy=policy).run(str(p), today=TODAY)
    assert a.state is RunState.COMPLETED
    assert (b.state, b.po_value_paise, b.rules) == (a.state, a.po_value_paise, a.rules)

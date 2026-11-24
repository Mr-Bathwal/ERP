"""Properties of the money path, generated rather than hand-picked.

v2's rounding test checked 0.125 and 0.135 and passed while 1.005 became
100 paise. A test that only checks the values its author thought of is a
test of the author; these check every value Hypothesis can think of.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest

hyp = pytest.importorskip("hypothesis")
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from procureflow.match import (  # noqa: E402
    Tolerances,
    dec,
    line_paise,
    lookalike_key,
    normalise_invoice_no,
    paise,
    rupees,
)

rupee_amounts = st.decimals(min_value=Decimal("-99999999.99"), max_value=Decimal("99999999.99"),
                            places=2, allow_nan=False, allow_infinity=False)
any_decimal = st.decimals(min_value=Decimal("-9999999"), max_value=Decimal("9999999"),
                          places=4, allow_nan=False, allow_infinity=False)


@given(rupee_amounts)
def test_paise_is_exact_for_every_two_decimal_amount(r):
    assert paise(str(r)) == int(r * 100)


@given(rupee_amounts)
def test_paise_survives_indian_grouping_and_the_rupee_sign(r):
    p = paise(str(r))
    assert paise("₹ " + rupees(p)) == p


@given(any_decimal)
def test_paise_rounds_half_away_from_zero(r):
    want = int((r * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert paise(str(r)) == want


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_a_float_is_read_as_the_number_it_prints_as(f):
    assert dec(f) == Decimal(repr(f))


@given(st.integers(min_value=0, max_value=10**9),
       st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=3,
                   allow_nan=False, allow_infinity=False))
def test_a_line_value_is_rounded_exactly_once(rate_p, qty):
    want = int((Decimal(rate_p) * qty).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    assert line_paise(rate_p, qty) == want


@given(st.integers(min_value=1, max_value=10**10))
def test_the_price_band_is_the_tighter_limit_and_never_negative(rate_p):
    tol = Tolerances()
    limit = tol.price_limit(rate_p)
    assert 0 <= limit <= tol.price_abs_paise
    assert limit <= tol.pct_limit_paise(rate_p)


@given(st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=3,
                   allow_nan=False, allow_infinity=False))
def test_the_quantity_band_is_the_greater_limit(q):
    tol = Tolerances()
    assert tol.qty_limit(q) >= Decimal(1)
    assert tol.qty_limit(q) >= q * 2 / 100


@given(st.text(max_size=30))
def test_invoice_keys_are_idempotent_and_never_crash(raw):
    k = normalise_invoice_no(raw)
    assert normalise_invoice_no(k) == k
    assert lookalike_key(lookalike_key(raw)) == lookalike_key(raw)

"""Tests for analytics/position_sizing.py — fixed-fractional (2%) sizing.

Fully offline pure-arithmetic tests; money asserted as Decimal.
"""

from __future__ import annotations

from decimal import Decimal

from analytics.risk.position_sizing import (
    DEFAULT_RISK_PCT,
    size_shares,
    size_contracts,
)


def test_default_risk_is_two_percent():
    assert DEFAULT_RISK_PCT == Decimal("2")


def test_size_shares_basic():
    r = size_shares(10000, 100, 95)  # $200 budget / $5 risk = 40 shares
    assert r.units == 40
    assert r.risk_budget == Decimal("200")
    assert r.actual_risk == Decimal("200")
    assert r.actual_risk_pct == Decimal("2.000")


def test_size_shares_floors_partial():
    r = size_shares(10000, 100, 94)  # budget 200 / risk 6 = 33.3 -> 33
    assert r.units == 33
    assert r.actual_risk == Decimal("198")


def test_size_shares_custom_risk_pct():
    r = size_shares(10000, 100, 95, Decimal("1"))
    assert r.units == 20  # $100 budget / $5


def test_size_shares_cash_capped():
    r = size_shares(1000, 100, 99.9)  # tiny stop would allow huge size; cash caps
    assert r.units == 10
    assert "Capped by cash" in r.note


def test_size_shares_too_small_budget():
    r = size_shares(100, 100, 50)  # risk/share $50 > $2 budget
    assert r.units == 0
    assert "risks more than the budget" in r.note


def test_size_shares_equal_entry_stop():
    r = size_shares(10000, 100, 100)
    assert r.units == 0
    assert "no defined risk" in r.note


def test_no_float_artifact_in_money():
    # 0.1 + 0.2 style inputs must not leak binary float noise into the risk figure.
    r = size_shares("10000", "100.30", "100.10")
    assert r.risk_per_unit == Decimal("0.20")


def test_size_contracts_long_option():
    r = size_contracts(20000, 200)  # $400 budget / $200 -> 2 contracts
    assert r.units == 2
    assert r.actual_risk == Decimal("400")


def test_size_contracts_defined_risk_spread():
    r = size_contracts(30000, 350)  # $600 budget / $350 -> 1
    assert r.units == 1


def test_size_contracts_too_large():
    r = size_contracts(5000, 500)  # $100 budget
    assert r.units == 0
    assert "exceeds the risk budget" in r.note


def test_bad_equity():
    assert size_shares(0, 100, 95).units == 0
    assert size_contracts(-100, 200).units == 0

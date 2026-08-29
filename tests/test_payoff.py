"""Offline tests for the pure option payoff calculator."""

from datetime import date
from decimal import Decimal

import pytest

from analytics.payoff import (
    OptionLeg,
    bear_put_spread,
    bull_call_spread,
    call_credit_spread,
    cash_secured_put,
    covered_call,
    long_call,
    long_put,
    payoff_at_expiry,
    put_credit_spread,
    short_call,
    summarize_expiry,
)


def test_long_call_has_defined_loss_unbounded_profit_and_breakeven():
    leg = long_call("100", "3")
    result = summarize_expiry([leg])
    assert result.net_credit == Decimal("-300")
    assert result.max_loss == Decimal("300")
    assert result.max_profit is None
    assert result.breakevens == (Decimal("103"),)
    assert payoff_at_expiry("110", [leg]) == Decimal("700")


def test_long_put_has_defined_loss_and_bounded_profit():
    result = summarize_expiry([long_put(100, 4)])
    assert result.max_loss == Decimal("400")
    assert result.max_profit == Decimal("9600")
    assert result.breakevens == (Decimal("96"),)


def test_bull_call_spread_calculates_debit_breakeven_and_capped_reward():
    legs = bull_call_spread(100, 5, 110, 2)
    result = summarize_expiry(legs)
    assert result.net_credit == Decimal("-300")
    assert result.max_loss == Decimal("300")
    assert result.max_profit == Decimal("700")
    assert result.breakevens == (Decimal("103"),)


def test_credit_spreads_have_defined_risk():
    put_result = summarize_expiry(put_credit_spread(100, 3, 95, 1))
    call_result = summarize_expiry(call_credit_spread(100, 3, 105, 1))
    for result in (put_result, call_result):
        assert result.net_credit == Decimal("200")
        assert result.max_profit == Decimal("200")
        assert result.max_loss == Decimal("300")


def test_iron_condor_is_a_defined_risk_four_leg_strategy():
    legs = (*put_credit_spread(95, 2, 90, 1), *call_credit_spread(105, 2, 110, 1))
    result = summarize_expiry(legs)
    assert result.net_credit == Decimal("200")
    assert result.max_profit == Decimal("200")
    assert result.max_loss == Decimal("300")
    assert result.breakevens == (Decimal("93"), Decimal("107"))


def test_cash_secured_put_reports_assignment_notional():
    result = summarize_expiry([cash_secured_put(80, 2)])
    assert result.net_credit == Decimal("200")
    assert result.max_profit == Decimal("200")
    assert result.max_loss == Decimal("7800")
    assert result.assignment_notional == Decimal("8000")


def test_covered_call_requires_coverage_and_has_capped_profit():
    stock, call = covered_call(90, 100, 100, 2)
    result = summarize_expiry([call], [stock])
    assert result.max_profit == Decimal("1200")
    assert result.max_loss == Decimal("8800")
    assert payoff_at_expiry(100, [call], [stock]) == Decimal("1200")
    with pytest.raises(ValueError, match="requires"):
        covered_call(90, 99, 100, 2)


def test_short_naked_call_has_unbounded_loss():
    result = summarize_expiry([short_call(100, 3)])
    assert result.max_profit == Decimal("300")
    assert result.max_loss is None


def test_non_standard_multiplier_and_decimal_quantity_are_respected():
    leg = long_call("10", "1.25", quantity="2.5", multiplier="10")
    assert payoff_at_expiry("15", [leg]) == Decimal("93.75")


def test_rejects_mixed_expiries_and_invalid_spread_geometry():
    with pytest.raises(ValueError, match="common expiry"):
        summarize_expiry([
            long_call(100, 2, expiry=date(2026, 9, 1)),
            short_call(110, 1, expiry=date(2026, 10, 1)),
        ])
    with pytest.raises(ValueError, match="long_strike"):
        bull_call_spread(110, 2, 100, 1)


def test_rejects_invalid_contract_data_and_negative_underlying_price():
    with pytest.raises(ValueError, match="positive"):
        OptionLeg("call", "buy", 100, 1, multiplier=0)
    with pytest.raises(ValueError, match="non-negative"):
        payoff_at_expiry(-1, [long_call(100, 2)])

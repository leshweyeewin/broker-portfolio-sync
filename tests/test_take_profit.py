"""Tests for alerting/take_profit.py — live long-option take-profit alert.

Offline: builds Position objects directly and calls the pure evaluator; no
adapters or network involved.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from adapters.base import AssetType, Broker, OptionType, Position
from alerting.take_profit import (
    DEFAULT_TP_PCT,
    evaluate_take_profits,
    format_message,
    unrealized_pct,
)


def _opt(qty, avg_cost, market_price, *, option_type=OptionType.CALL, symbol="AVGO"):
    return Position(
        broker=Broker.MOOMOO,
        asset_type=AssetType.OPTION,
        symbol=symbol,
        qty=qty,
        avg_cost=avg_cost,
        currency="USD",
        market_price=market_price,
        option_type=option_type,
        strike=Decimal("370"),
        expiry=date(2026, 9, 4),
    )


def _stock(qty, avg_cost, market_price, symbol="AAPL"):
    return Position(
        broker=Broker.MOOMOO, asset_type=AssetType.STOCK, symbol=symbol,
        qty=qty, avg_cost=avg_cost, currency="USD", market_price=market_price,
    )


def test_long_call_above_target_fires():
    sig = evaluate_take_profits([_opt(2, 5, 8)])  # +60%
    assert len(sig) == 1
    assert sig[0].symbol == "AVGO"
    assert sig[0].option_type == "Call"


def test_long_call_at_exactly_50_fires():
    sig = evaluate_take_profits([_opt(1, 5, "7.5")])  # +50%
    assert len(sig) == 1


def test_long_call_below_target_silent():
    assert evaluate_take_profits([_opt(1, 5, 7)]) == []  # +40%


def test_short_option_ignored():
    # A short option up big is not a long-take-profit case.
    assert evaluate_take_profits([_opt(-1, 5, 1)]) == []


def test_stock_ignored():
    assert evaluate_take_profits([_stock(100, 100, 200)]) == []


def test_missing_market_price_skipped():
    assert evaluate_take_profits([_opt(1, 5, None)]) == []


def test_long_put_also_fires():
    sig = evaluate_take_profits([_opt(1, 2, 3, option_type=OptionType.PUT)])  # +50%
    assert len(sig) == 1
    assert sig[0].option_type == "Put"


def test_custom_threshold():
    assert evaluate_take_profits([_opt(1, 5, 6)], tp_pct=Decimal("15")) != []  # +20% >= 15


def test_unrealized_pct_zero_cost_none():
    assert unrealized_pct(_opt(1, 0, 5)) is None


def test_default_threshold_is_50():
    assert DEFAULT_TP_PCT == Decimal("50")


def test_format_message_has_count_and_symbol():
    msg = format_message(evaluate_take_profits([_opt(2, 5, 8)]))
    assert "1 at/above" in msg
    assert "AVGO" in msg

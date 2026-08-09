"""Unit tests for the common schema (adapters/base.py) — no broker SDK needed."""

from datetime import date
from decimal import Decimal

import pytest

from adapters.base import (
    Broker,
    CashMovement,
    CashType,
    Direction,
    OptionAction,
    OptionTrade,
    OptionType,
    StockAction,
    StockTrade,
    dec,
    make_dedup_key,
    opening_dedup_key,
)


# --- dec() ----------------------------------------------------------------- #
def test_dec_routes_float_via_repr_no_binary_artifacts():
    assert dec(0.1) == Decimal("0.1")
    assert dec(0.1) + dec(0.2) == Decimal("0.3")


def test_dec_accepts_int_str_decimal():
    assert dec(5) == Decimal("5")
    assert dec("2.50") == Decimal("2.50")
    assert dec(Decimal("1.23")) == Decimal("1.23")


def test_dec_rejects_none_and_bool():
    with pytest.raises(ValueError):
        dec(None)
    with pytest.raises(TypeError):
        dec(True)


# --- sign convention ------------------------------------------------------- #
def test_stock_buy_total_is_negative_outflow():
    t = StockTrade(
        date=date(2026, 1, 2),
        broker=Broker.TIGER,
        ticker="AAPL",
        action=StockAction.BUY,
        qty=10,
        price="150.00",
        currency="usd",
    )
    assert t.total == Decimal("-1500.00")
    assert t.currency == "USD"  # normalized upper


def test_stock_sell_total_is_positive_inflow():
    t = StockTrade(
        date=date(2026, 1, 3),
        broker=Broker.TIGER,
        ticker=" AAPL ",
        action=StockAction.SELL,
        qty=4,
        price="200",
        currency="USD",
    )
    assert t.total == Decimal("800")
    assert t.ticker == "AAPL"  # whitespace stripped


def test_opening_balance_treated_as_acquisition():
    t = StockTrade(
        date=date(2026, 1, 1),
        broker=Broker.TIGER,
        ticker="MSFT",
        action=StockAction.OPENING_BALANCE,
        qty=3,
        price="100",
        currency="USD",
    )
    assert t.total == Decimal("-300")
    assert t.dedup_key == "Tiger:opening:MSFT"


def test_option_sell_premium_is_credit_with_multiplier():
    t = OptionTrade(
        date=date(2026, 1, 2),
        broker=Broker.TIGER,
        underlying="SPY",
        option_type=OptionType.PUT,
        strike="400",
        qty=2,
        expiry=date(2026, 3, 20),
        action=OptionAction.SELL,
        premium="1.50",
        currency="USD",
        direction=Direction.BULLISH,
    )
    # credit received = 1.50 * 2 * 100
    assert t.total == Decimal("300.00")


# --- dedup keys ------------------------------------------------------------ #
def test_dedup_prefers_fill_id():
    assert make_dedup_key(Broker.TIGER, "abc123", "ignored") == "Tiger:abc123"


def test_dedup_hash_is_deterministic_and_broker_prefixed():
    k1 = make_dedup_key(Broker.TIGER, None, "2026-01-02", "AAPL", "Buy", 10, 150)
    k2 = make_dedup_key(Broker.TIGER, None, "2026-01-02", "AAPL", "Buy", 10, 150)
    assert k1 == k2
    assert k1.startswith("Tiger:")
    assert make_dedup_key(Broker.MOOMOO, None, "x") != make_dedup_key(
        Broker.TIGER, None, "x"
    )


def test_trade_without_fill_id_hashes_business_fields():
    a = StockTrade(
        date=date(2026, 1, 2),
        broker=Broker.TIGER,
        ticker="AAPL",
        action=StockAction.BUY,
        qty=10,
        price="150",
        currency="USD",
    )
    b = StockTrade(
        date=date(2026, 1, 2),
        broker=Broker.TIGER,
        ticker="AAPL",
        action=StockAction.BUY,
        qty=10,
        price="150",
        currency="USD",
    )
    assert a.dedup_key == b.dedup_key  # same execution -> same key (idempotent)


def test_option_opening_key_distinguishes_contracts():
    k_call = opening_dedup_key(Broker.TIGER, "SPY:Call:400:2026-03-20")
    k_put = opening_dedup_key(Broker.TIGER, "SPY:Put:400:2026-03-20")
    assert k_call != k_put


# --- cash movements -------------------------------------------------------- #
def test_cash_type_external_capital_flag():
    assert CashType.DEPOSIT.is_external_capital
    assert CashType.WITHDRAWAL.is_external_capital
    assert not CashType.DIVIDEND.is_external_capital
    assert not CashType.FX_CONVERSION.is_external_capital


def test_cash_movement_amount_is_decimal_and_currency_normalized():
    m = CashMovement(
        date=date(2026, 1, 2),
        broker=Broker.TIGER,
        type=CashType.DEPOSIT,
        amount="1000.00",
        currency="sgd",
    )
    assert m.amount == Decimal("1000.00")
    assert m.currency == "SGD"
    assert m.dedup_key.startswith("Tiger:")

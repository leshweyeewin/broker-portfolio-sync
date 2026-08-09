"""Unit tests for the FIFO P/L engine (core/fifo_pl.py) — pure, offline."""

from datetime import date
from decimal import Decimal

import pytest

from adapters.base import (
    Broker,
    OptionAction,
    OptionTrade,
    OptionType,
    StockAction,
    StockTrade,
)
from core.fifo_pl import compute_option_pl, compute_stock_pl


def _stk(action, qty, price, fee="0", d=(2026, 1, 1), ticker="AAPL"):
    return StockTrade(
        date=date(*d),
        broker=Broker.TIGER,
        ticker=ticker,
        action=action,
        qty=qty,
        price=price,
        fee=fee,
        currency="USD",
    )


def _opt(action, qty, premium, fee="0", d=(2026, 1, 1), strike="400", exp=(2026, 3, 20)):
    return OptionTrade(
        date=date(*d),
        broker=Broker.TIGER,
        underlying="SPY",
        option_type=OptionType.PUT,
        strike=strike,
        qty=qty,
        expiry=date(*exp),
        action=action,
        premium=premium,
        fee=fee,
        currency="USD",
    )


# --- basic long stock FIFO ------------------------------------------------- #
def test_fifo_partial_sell_across_two_lots_net_of_fees():
    trades = [
        _stk(StockAction.BUY, 10, "100", fee="5", d=(2026, 1, 1)),
        _stk(StockAction.BUY, 10, "110", fee="5", d=(2026, 1, 2)),
        _stk(StockAction.SELL, 15, "120", fee="3", d=(2026, 1, 3)),
    ]
    res = compute_stock_pl(trades)
    assert len(res.realizations) == 1
    r = res.realizations[0]
    # gross = (120-100)*10 + (120-110)*5 = 250
    # open fees released = 0.5*10 + 0.5*5 = 7.5 ; close fee = 3
    assert r.realized_pl == Decimal("239.5")
    assert r.qty == Decimal("15")
    assert r.cost_basis == Decimal("1550")  # 100*10 + 110*5
    # remaining holding: 5 @ 110, fee-inclusive avg cost 110.5
    assert len(res.holdings) == 1
    h = res.holdings[0]
    assert h.qty == Decimal("5")
    assert h.avg_price == Decimal("110")
    assert h.avg_cost == Decimal("110.5")
    assert h.unrealized_pl("130") == Decimal("97.5")  # (130-110)*5 - 2.5


def test_realized_by_key_targets_the_sell_row():
    buy = _stk(StockAction.BUY, 5, "10", d=(2026, 1, 1))
    sell = _stk(StockAction.SELL, 5, "12", d=(2026, 1, 2))
    res = compute_stock_pl([buy, sell])
    assert set(res.realized_by_key) == {sell.dedup_key}
    assert res.total_realized == Decimal("10")  # (12-10)*5


# --- opening balance seed (§5) --------------------------------------------- #
def test_opening_balance_seeds_cost_basis():
    trades = [
        _stk(StockAction.OPENING_BALANCE, 10, "100", d=(2026, 1, 1)),
        _stk(StockAction.SELL, 4, "120", fee="1", d=(2026, 2, 1)),
    ]
    res = compute_stock_pl(trades)
    (r,) = res.realizations
    assert r.realized_pl == Decimal("79")  # (120-100)*4 - 1
    (h,) = res.holdings
    assert h.qty == Decimal("6") and h.avg_price == Decimal("100")


# --- short option (sell-to-open then buy-to-close) ------------------------- #
def test_short_put_credit_then_buy_to_close():
    trades = [
        _opt(OptionAction.SELL, 2, "1.50", fee="1", d=(2026, 1, 2)),  # open short
        _opt(OptionAction.BUY, 2, "0.50", fee="1", d=(2026, 1, 20)),  # close
    ]
    res = compute_option_pl(trades)
    (r,) = res.realizations
    # gross = (1.50-0.50)*2*100 = 200 ; minus open fee 1 minus close fee 1
    assert r.realized_pl == Decimal("198")
    assert res.holdings == []  # fully closed


def test_short_option_opening_balance_negative_qty_seeds_short():
    trades = [
        _opt(OptionAction.OPENING_BALANCE, -1, "2.00", d=(2026, 1, 1)),  # short seed
        _opt(OptionAction.BUY, 1, "0.80", fee="0.5", d=(2026, 1, 15)),  # buy to close
    ]
    res = compute_option_pl(trades)
    (r,) = res.realizations
    # short credit 2.00, bought back 0.80: (2.00-0.80)*1*100 = 120 - 0.5 fee
    assert r.realized_pl == Decimal("119.5")
    assert res.holdings == []


# --- position flip --------------------------------------------------------- #
def test_sell_overshoot_flips_long_to_short():
    trades = [
        _stk(StockAction.BUY, 5, "100", d=(2026, 1, 1)),
        _stk(StockAction.SELL, 8, "110", d=(2026, 1, 2)),  # closes 5, opens 3 short
    ]
    res = compute_stock_pl(trades)
    (r,) = res.realizations
    assert r.qty == Decimal("5")
    assert r.realized_pl == Decimal("50")  # (110-100)*5
    (h,) = res.holdings
    assert h.qty == Decimal("-3")  # now short 3
    assert h.avg_price == Decimal("110")


# --- grouping & isolation -------------------------------------------------- #
def test_separate_tickers_are_independent_books():
    trades = [
        _stk(StockAction.BUY, 1, "10", ticker="AAA", d=(2026, 1, 1)),
        _stk(StockAction.BUY, 1, "20", ticker="BBB", d=(2026, 1, 1)),
        _stk(StockAction.SELL, 1, "15", ticker="AAA", d=(2026, 1, 2)),
    ]
    res = compute_stock_pl(trades)
    assert res.total_realized == Decimal("5")  # only AAA closed
    tickers = {h.instrument for h in res.holdings}
    assert tickers == {"BBB"}


def test_distinct_option_contracts_do_not_net():
    trades = [
        _opt(OptionAction.SELL, 1, "1.00", strike="400", d=(2026, 1, 1)),
        _opt(OptionAction.SELL, 1, "1.00", strike="410", d=(2026, 1, 1)),
    ]
    res = compute_option_pl(trades)
    assert len(res.holdings) == 2  # two distinct strikes, both open short


# --- fail loud ------------------------------------------------------------- #
def test_mixed_currency_same_ticker_fails_loud():
    t1 = _stk(StockAction.BUY, 1, "10")
    t2 = StockTrade(
        date=date(2026, 1, 2), broker=Broker.TIGER, ticker="AAPL",
        action=StockAction.SELL, qty=1, price="12", currency="HKD",
    )
    with pytest.raises(ValueError, match="mixed currencies"):
        compute_stock_pl([t1, t2])


def test_empty_input_yields_empty_result():
    res = compute_stock_pl([])
    assert res.realizations == [] and res.holdings == []
    assert res.total_realized == Decimal("0")

"""MooMoo adapter mapping tests using a fake OpenD trade context (no gateway).

Validates the SDK-shaped DataFrames map into the common schema correctly.
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from moomoo import RET_OK

from adapters.base import (
    AssetType,
    Broker,
    OptionAction,
    OptionType,
    StockAction,
)
from adapters.moomoo import MooMooAdapter


class FakeCtx:
    """Stands in for OpenSecTradeContext; returns (RET_OK, DataFrame) like the SDK."""

    def __init__(self, orders=None, fees=None, positions=None, fail=None):
        self._orders = pd.DataFrame(orders or [])
        self._fees = pd.DataFrame(fees or [])
        self._positions = pd.DataFrame(positions or [])
        self._fail = fail  # name of a method that should return a non-OK code
        self.closed = False

    def close(self):
        self.closed = True

    def history_order_list_query(self, status_filter_list, start, end, trd_env, acc_id):
        if self._fail == "history_order_list_query":
            return (-1, "boom")
        return (RET_OK, self._orders)

    def order_fee_query(self, order_id_list, trd_env, acc_id):
        return (RET_OK, self._fees)

    def position_list_query(self, trd_env, acc_id):
        if self._fail == "position_list_query":
            return (-1, "boom")
        return (RET_OK, self._positions)


def _adapter(ctx):
    return MooMooAdapter(context_factory=lambda market: ctx, markets=("US",))


# --- stock executions ------------------------------------------------------ #
def test_stock_buy_execution_with_fee_join():
    ctx = FakeCtx(
        orders=[
            {
                "order_id": "OID1",
                "code": "US.AAPL",
                "trd_side": "BUY",
                "dealt_qty": 10,
                "dealt_avg_price": 150.0,
                "currency": "USD",
                "updated_time": "2026-01-02 10:00:00",
            }
        ],
        fees=[{"order_id": "OID1", "fee_amount": 1.99}],
    )
    (t,) = _adapter(ctx).fetch_stock_executions(since=None)
    assert t.broker is Broker.MOOMOO
    assert t.ticker == "AAPL"
    assert t.action is StockAction.BUY
    assert t.qty == Decimal("10")
    assert t.price == Decimal("150.0")
    assert t.fee == Decimal("1.99")
    assert t.total == Decimal("-1500.0")  # buy -> outflow
    assert t.currency == "USD"
    assert t.date == date(2026, 1, 2)
    assert t.dedup_key == "MooMoo:OID1"


def test_stock_missing_fee_defaults_zero_and_options_excluded():
    ctx = FakeCtx(
        orders=[
            {"order_id": "S1", "code": "HK.00700", "trd_side": "SELL", "dealt_qty": 100,
             "dealt_avg_price": 400.0, "currency": "HKD", "updated_time": "2026-02-01 09:30:00"},
            {"order_id": "O1", "code": "US.AAPL240119C00190000", "trd_side": "SELL_SHORT",
             "dealt_qty": 1, "dealt_avg_price": 2.0, "currency": "USD", "updated_time": "2026-02-01 09:30:00"},
        ],
        fees=[],  # no fee rows at all
    )
    trades = _adapter(ctx).fetch_stock_executions(since=None)
    assert len(trades) == 1  # the option row is excluded
    assert trades[0].ticker == "00700"
    assert trades[0].fee == Decimal("0")
    assert trades[0].total == Decimal("40000.0")  # sell -> inflow


# --- option executions ----------------------------------------------------- #
def test_option_sell_short_parsed_from_occ_code():
    ctx = FakeCtx(
        orders=[
            {"order_id": "O9", "code": "US.AAPL240119C00190000", "trd_side": "SELL_SHORT",
             "dealt_qty": 2, "dealt_avg_price": 1.5, "currency": "USD",
             "updated_time": "2026-01-05 11:00:00"},
        ],
        fees=[{"order_id": "O9", "fee_amount": 1.30}],
    )
    (t,) = _adapter(ctx).fetch_option_executions(since=None)
    assert t.underlying == "AAPL"
    assert t.option_type is OptionType.CALL
    assert t.strike == Decimal("190")
    assert t.expiry == date(2024, 1, 19)
    assert t.action is OptionAction.SELL  # sell-to-open
    assert t.multiplier == Decimal("100")
    assert t.fee == Decimal("1.30")
    assert t.total == Decimal("300.0")  # 1.5 * 2 * 100 credit


def test_buy_back_is_treated_as_buy():
    ctx = FakeCtx(
        orders=[
            {"order_id": "O10", "code": "US.SPY251219P00400000", "trd_side": "BUY_BACK",
             "dealt_qty": 1, "dealt_avg_price": 0.5, "currency": "USD",
             "updated_time": "2026-01-20 10:00:00"},
        ],
    )
    (t,) = _adapter(ctx).fetch_option_executions(since=None)
    assert t.action is OptionAction.BUY
    assert t.option_type is OptionType.PUT


# --- positions ------------------------------------------------------------- #
def test_positions_long_stock_and_short_option_sign():
    ctx = FakeCtx(
        positions=[
            {"code": "US.NVDA", "qty": 8, "cost_price": 100.0, "nominal_price": 120.0,
             "currency": "USD", "position_side": "LONG"},
            # short option: currency column absent -> derived from US prefix
            {"code": "US.SPY251219P00400000", "qty": 3, "cost_price": 2.0,
             "nominal_price": 1.0, "position_side": "SHORT"},
        ]
    )
    positions = _adapter(ctx).fetch_positions()
    assert len(positions) == 2
    stk = next(p for p in positions if p.asset_type is AssetType.STOCK)
    opt = next(p for p in positions if p.asset_type is AssetType.OPTION)
    assert stk.symbol == "NVDA" and stk.qty == Decimal("8")
    assert stk.market_price == Decimal("120.0")
    assert opt.qty == Decimal("-3")  # short -> negative
    assert opt.symbol == "SPY" and opt.option_type is OptionType.PUT
    assert opt.strike == Decimal("400") and opt.expiry == date(2025, 12, 19)
    assert opt.currency == "USD"  # derived from code prefix


# --- cash & robustness ----------------------------------------------------- #
def test_cash_movements_always_empty():
    ctx = FakeCtx()
    assert _adapter(ctx).fetch_cash_movements(since=None) == []


def test_non_ok_return_fails_loud():
    ctx = FakeCtx(fail="history_order_list_query")
    with pytest.raises(RuntimeError, match="history_order_list_query failed"):
        _adapter(ctx).fetch_stock_executions(since=None)
    ctx2 = FakeCtx(fail="position_list_query")
    with pytest.raises(RuntimeError, match="position_list_query failed"):
        _adapter(ctx2).fetch_positions()


def test_multi_market_queries_each_context():
    us = FakeCtx(orders=[{"order_id": "U1", "code": "US.AAPL", "trd_side": "BUY",
                          "dealt_qty": 1, "dealt_avg_price": 10.0, "currency": "USD",
                          "updated_time": "2026-01-02 10:00:00"}])
    hk = FakeCtx(orders=[{"order_id": "H1", "code": "HK.00700", "trd_side": "BUY",
                          "dealt_qty": 1, "dealt_avg_price": 20.0, "currency": "HKD",
                          "updated_time": "2026-01-02 10:00:00"}])
    ctxs = {"US": us, "HK": hk}
    adapter = MooMooAdapter(context_factory=lambda m: ctxs[m], markets=("US", "HK"))
    trades = adapter.fetch_stock_executions(since=None)
    assert {t.currency for t in trades} == {"USD", "HKD"}
    assert {t.dedup_key for t in trades} == {"MooMoo:U1", "MooMoo:H1"}


def test_close_closes_and_clears_contexts():
    """close() must release every cached context — leaked OpenD connections can
    wedge the gateway across runs (root cause of a real 6-hour hang)."""
    us, hk = FakeCtx(), FakeCtx()
    ctxs = {"US": us, "HK": hk}
    adapter = MooMooAdapter(context_factory=lambda m: ctxs[m], markets=("US", "HK"))
    adapter.fetch_positions()  # materialises both contexts
    adapter.close()
    assert us.closed is True and hk.closed is True
    assert adapter._ctx_cache == {}


def test_adapter_satisfies_protocol():
    from adapters.base import BrokerAdapter

    a = _adapter(FakeCtx())
    assert isinstance(a, BrokerAdapter)
    assert a.name == "MooMoo"

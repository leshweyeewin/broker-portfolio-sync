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
from adapters.moomoo import MooMooAdapter, _combo_leg_buy_flags
from adapters.base import parse_option_legs


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


def test_moomoo_option_code_variable_strike_parsed_as_option():
    """MooMoo option codes aren't zero-padded to 8 strike digits
    (e.g. US.SHOP260821C145000 = 145.00 strike) — they must still parse as
    options, not fall through as stocks (real bug: shorts landed in Stocks)."""
    ctx = FakeCtx(
        positions=[
            {"code": "US.SHOP260821C145000", "qty": -1, "cost_price": 7.13,
             "nominal_price": 6.0, "currency": "USD", "position_side": "SHORT"},
        ]
    )
    (p,) = _adapter(ctx).fetch_positions()
    assert p.asset_type is AssetType.OPTION
    assert p.symbol == "SHOP"
    assert p.option_type is OptionType.CALL
    assert p.strike == Decimal("145")
    assert p.expiry == date(2026, 8, 21)
    assert p.qty == Decimal("-1")  # short


def test_positions_deduped_across_markets():
    """OpenD returns the full position list for every market context, so querying
    US+HK must not double-count (real bug: 11 positions became 22)."""
    same = [
        {"code": "US.SOXL", "qty": 100, "cost_price": 114.99, "nominal_price": 60.0,
         "currency": "USD", "position_side": "LONG"},
    ]
    us, hk = FakeCtx(positions=same), FakeCtx(positions=same)
    ctxs = {"US": us, "HK": hk}
    adapter = MooMooAdapter(context_factory=lambda m: ctxs[m], markets=("US", "HK"))
    positions = adapter.fetch_positions()
    assert len(positions) == 1  # not 2
    assert positions[0].symbol == "SOXL" and positions[0].qty == Decimal("100")


def test_executions_deduped_across_markets():
    same = [{"order_id": "X1", "code": "US.AAPL", "trd_side": "BUY", "dealt_qty": 1,
             "dealt_avg_price": 10.0, "currency": "USD", "updated_time": "2026-01-02 10:00:00"}]
    us, hk = FakeCtx(orders=same), FakeCtx(orders=same)
    ctxs = {"US": us, "HK": hk}
    adapter = MooMooAdapter(context_factory=lambda m: ctxs[m], markets=("US", "HK"))
    trades = adapter.fetch_stock_executions(since=None)
    assert len(trades) == 1  # same order_id from both markets -> one trade


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


def test_moomoo_combo_spread_decomposed_into_legs():
    # BUY vertical put spread US.SHOP260821P130/145 x 1 @ 2.50, fee 1.50
    ctx = FakeCtx(
        orders=[
            {
                "order_id": "O_SHOP_SPREAD",
                "code": "US.SHOP260821P130/145",
                "trd_side": "BUY",
                "dealt_qty": 1,
                "dealt_avg_price": 2.50,
                "currency": "USD",
                "updated_time": "2026-01-05 10:00:00",
            }
        ],
        fees=[{"order_id": "O_SHOP_SPREAD", "fee_amount": 1.50}],
    )
    # Must NOT land in stock executions
    stk_trades = _adapter(ctx).fetch_stock_executions(since=None)
    assert len(stk_trades) == 0

    # Must decompose into 2 OptionTrades
    opt_trades = _adapter(ctx).fetch_option_executions(since=None)
    assert len(opt_trades) == 2

    # Buying a put vertical is long the HIGHER strike (145), short the lower (130)
    # — so it closes a held spread rather than doubling it. Premium/fee land on the
    # long (order-side) leg so the net debit keeps its sign.
    leg0, leg1 = opt_trades
    assert leg0.underlying == "SHOP" and leg0.strike == Decimal("130")
    assert leg0.option_type is OptionType.PUT
    assert leg0.expiry == date(2026, 8, 21)
    assert leg0.action is OptionAction.SELL   # lower strike = short leg
    assert leg0.qty == Decimal("1")
    assert leg0.premium == Decimal("0")
    assert leg0.fee == Decimal("0")
    assert leg0.total == Decimal("0")
    assert leg0.dedup_key == "MooMoo:O_SHOP_SPREAD:0"

    assert leg1.underlying == "SHOP" and leg1.strike == Decimal("145")
    assert leg1.option_type is OptionType.PUT
    assert leg1.expiry == date(2026, 8, 21)
    assert leg1.action is OptionAction.BUY    # higher strike = long leg
    assert leg1.qty == Decimal("1")
    assert leg1.premium == Decimal("2.50")
    assert leg1.fee == Decimal("1.50")
    assert leg1.total == Decimal("-250.00")   # BUY outflow (net debit)
    assert leg1.dedup_key == "MooMoo:O_SHOP_SPREAD:1"


def test_moomoo_combo_spread_with_full_zeros_strike():
    # US.SHOP260821P130000/145000 with SELL trd_side
    ctx = FakeCtx(
        orders=[
            {
                "order_id": "O_SELL_SPREAD",
                "code": "US.SHOP260821P130000/145000",
                "trd_side": "SELL",
                "dealt_qty": 2,
                "dealt_avg_price": 3.00,
                "currency": "USD",
                "updated_time": "2026-01-05 10:00:00",
            }
        ],
        fees=[{"order_id": "O_SELL_SPREAD", "fee_amount": 2.00}],
    )
    opt_trades = _adapter(ctx).fetch_option_executions(since=None)
    assert len(opt_trades) == 2

    # Selling the put vertical flips both legs: short the higher strike (145),
    # long the lower (130). Premium/fee land on the order-side (145 sell) leg.
    leg0, leg1 = opt_trades
    assert leg0.strike == Decimal("130") and leg0.action is OptionAction.BUY
    assert leg0.qty == Decimal("2")
    assert leg0.premium == Decimal("0")
    assert leg0.fee == Decimal("0")
    assert leg0.total == Decimal("0")

    assert leg1.strike == Decimal("145") and leg1.action is OptionAction.SELL
    assert leg1.qty == Decimal("2")
    assert leg1.premium == Decimal("3.00")
    assert leg1.fee == Decimal("2.00")
    assert leg1.total == Decimal("600.00")  # SELL inflow (3 * 2 * 100)


def test_moomoo_fractional_and_slashed_positions():
    ctx = FakeCtx(
        positions=[
            {"code": "US.MARA260821C23500", "qty": -2, "cost_price": 1.5,
             "nominal_price": 1.2, "currency": "USD", "position_side": "SHORT"},
            {"code": "US.SHOP260821P130000/US.SHOP260821P145000", "qty": 1, "cost_price": 2.0,
             "nominal_price": 2.2, "currency": "USD", "position_side": "LONG"},
        ]
    )
    positions = _adapter(ctx).fetch_positions()
    assert len(positions) == 3

    mara = positions[0]
    assert mara.symbol == "MARA"
    assert mara.strike == Decimal("23.5")
    assert mara.qty == Decimal("-2")

    shop1, shop2 = positions[1], positions[2]
    assert shop1.symbol == "SHOP" and shop1.strike == Decimal("130")
    assert shop2.symbol == "SHOP" and shop2.strike == Decimal("145")



# --------------------------------------------------------------------------- #
# Combo-vertical leg direction (_combo_leg_buy_flags) — reconciliation fix
# --------------------------------------------------------------------------- #

def test_buy_put_vertical_is_long_higher_strike():
    # Buying SHOP P130/145: long the higher strike (145), short the lower (130) —
    # so it CLOSES a held put spread instead of doubling it.
    legs = parse_option_legs("SHOP260821P130/145")
    assert [leg[2] for leg in legs] == [Decimal(130), Decimal(145)]
    assert _combo_leg_buy_flags(legs, is_buy=True) == [False, True]   # 130 Sell, 145 Buy
    assert _combo_leg_buy_flags(legs, is_buy=False) == [True, False]  # selling flips both


def test_buy_call_vertical_is_long_lower_strike():
    legs = parse_option_legs("SHOP260821C145/160")
    assert _combo_leg_buy_flags(legs, is_buy=True) == [True, False]   # 145 Buy, 160 Sell


def test_non_vertical_combo_uses_leg0_fallback():
    # Mixed types (put + call) → not a vertical → leg 0 takes the order side.
    legs = [("SHOP", OptionType.PUT, Decimal(130), date(2026, 8, 21)),
            ("SHOP", OptionType.CALL, Decimal(145), date(2026, 8, 21))]
    assert _combo_leg_buy_flags(legs, is_buy=True) == [True, False]

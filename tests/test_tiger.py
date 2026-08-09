"""Tiger adapter mapping tests using a fake TradeClient (no network/creds).

Validates that the adapter turns SDK-shaped objects into the common schema
correctly — the end-to-end schema check that step 2 exists to provide.
"""

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from adapters.base import (
    AssetType,
    Broker,
    CashType,
    OptionAction,
    OptionType,
    StockAction,
)
from adapters.tiger import TigerAdapter

SGT = ZoneInfo("Asia/Singapore")


def _ms(y, m, d, hh=12):
    return int(datetime(y, m, d, hh, tzinfo=SGT).timestamp() * 1000)


def _contract(**kw):
    base = dict(
        symbol="AAPL",
        currency="USD",
        strike=None,
        put_call=None,
        expiry=None,
        multiplier=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _order(**kw):
    base = dict(
        id=1001,
        order_id=1001,
        action="BUY",
        filled=10,
        quantity=10,
        avg_fill_price=150.0,
        commission=1.0,
        gst=0.5,
        trade_time=_ms(2026, 1, 2),
        order_time=_ms(2026, 1, 2),
        contract=_contract(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeClient:
    def __init__(self, stock_orders=None, option_orders=None, positions=None, fund_df=None):
        self._stock_orders = stock_orders or []
        self._option_orders = option_orders or []
        self._positions = positions or {}
        self._fund_df = fund_df

    def get_filled_orders(self, sec_type=None, **kw):
        return self._stock_orders if str(sec_type).endswith("STK") else self._option_orders

    def get_positions(self, sec_type=None, **kw):
        key = "OPT" if str(sec_type).endswith("OPT") else "STK"
        return self._positions.get(key, [])

    def get_fund_details(self, seg_types=None, start_date=None, **kw):
        return self._fund_df


def _adapter(client):
    return TigerAdapter(client=client, timezone="Asia/Singapore")


# --- stock executions ------------------------------------------------------ #
def test_stock_buy_execution_mapping():
    a = _adapter(FakeClient(stock_orders=[_order()]))
    (t,) = a.fetch_stock_executions(since=None)
    assert t.broker is Broker.TIGER
    assert t.ticker == "AAPL"
    assert t.action is StockAction.BUY
    assert t.qty == Decimal("10")
    assert t.price == Decimal("150.0")
    assert t.fee == Decimal("1.5")  # commission + gst (§8)
    assert t.total == Decimal("-1500.0")  # buy -> outflow
    assert t.currency == "USD"
    assert t.date == date(2026, 1, 2)
    assert t.dedup_key == "Tiger:1001"  # broker's order id (§6)


def test_stock_sell_and_zero_fill_skipped():
    orders = [
        _order(id=2, action="SELL", filled=5, avg_fill_price=200.0),
        _order(id=3, filled=0),  # nothing executed -> skipped
    ]
    a = _adapter(FakeClient(stock_orders=orders))
    trades = a.fetch_stock_executions(since=None)
    assert len(trades) == 1
    assert trades[0].action is StockAction.SELL
    assert trades[0].total == Decimal("1000.0")  # sell -> inflow


# --- option executions ----------------------------------------------------- #
def test_option_sell_put_mapping():
    oc = _contract(symbol="SPY", put_call="PUT", strike=400.0, expiry="20260320", multiplier=100)
    a = _adapter(
        _client := FakeClient(
            option_orders=[_order(id=55, action="SELL", filled=2, avg_fill_price=1.5, contract=oc)]
        )
    )
    (t,) = a.fetch_option_executions(since=None)
    assert t.underlying == "SPY"
    assert t.option_type is OptionType.PUT
    assert t.strike == Decimal("400.0")
    assert t.expiry == date(2026, 3, 20)
    assert t.action is OptionAction.SELL
    assert t.multiplier == Decimal("100")
    assert t.total == Decimal("300.0")  # 1.5 * 2 * 100 credit


# --- positions ------------------------------------------------------------- #
def test_positions_stock_and_option():
    stock_pos = SimpleNamespace(
        contract=_contract(symbol="NVDA", currency="USD"),
        quantity=8,
        average_cost=100.0,
        market_price=120.0,
    )
    opt_pos = SimpleNamespace(
        contract=_contract(symbol="SPY", currency="USD", put_call="CALL", strike=500.0, expiry="20260618", multiplier=100),
        quantity=1,
        average_cost=3.25,
        market_price=4.0,
    )
    a = _adapter(FakeClient(positions={"STK": [stock_pos], "OPT": [opt_pos]}))
    positions = a.fetch_positions()
    assert len(positions) == 2
    stk = next(p for p in positions if p.asset_type is AssetType.STOCK)
    opt = next(p for p in positions if p.asset_type is AssetType.OPTION)
    assert stk.symbol == "NVDA" and stk.qty == Decimal("8") and stk.avg_cost == Decimal("100.0")
    assert opt.option_type is OptionType.CALL and opt.strike == Decimal("500.0")
    assert opt.expiry == date(2026, 6, 18)


# --- cash movements -------------------------------------------------------- #
def test_cash_movements_classification():
    df = pd.DataFrame(
        [
            {"id": 1, "currency": "SGD", "amount": 1000.0, "fund_type": "DEPOSIT", "settled_time": _ms(2026, 1, 2)},
            {"id": 2, "currency": "USD", "amount": -50.0, "fund_type": "WITHDRAWAL", "settled_time": _ms(2026, 1, 3)},
            {"id": 3, "currency": "USD", "amount": 12.5, "fund_type": "CASH_DIVIDEND", "settled_time": _ms(2026, 1, 4)},
            {"id": 4, "currency": "USD", "amount": 7.0, "fund_type": "MYSTERY", "settled_time": _ms(2026, 1, 5)},
        ]
    )
    a = _adapter(FakeClient(fund_df=df))
    moves = a.fetch_cash_movements(since=None)
    assert len(moves) == 4
    by_type = {m.type for m in moves}
    assert CashType.DEPOSIT in by_type and CashType.WITHDRAWAL in by_type
    assert CashType.DIVIDEND in by_type
    # withdrawal amount stored positive (Transactions tab is positive)
    wd = next(m for m in moves if m.type is CashType.WITHDRAWAL)
    assert wd.amount == Decimal("50.0")
    # unknown type kept, classified out of external capital, tagged in note
    mystery = next(m for m in moves if "unmapped" in m.note)
    assert mystery.type is CashType.INTERNAL_TRANSFER
    assert not mystery.type.is_external_capital


def test_cash_movements_disabled_returns_empty():
    a = TigerAdapter(client=FakeClient(fund_df=pd.DataFrame([{"x": 1}])), cash_movements_enabled=False)
    assert a.fetch_cash_movements(since=None) == []


def test_cash_movements_unknown_schema_fails_loud():
    bad = pd.DataFrame([{"foo": 1, "bar": 2}])  # no amount/currency/type/date cols
    a = _adapter(FakeClient(fund_df=bad))
    with pytest.raises(ValueError, match="missing expected column"):
        a.fetch_cash_movements(since=None)


# --- protocol conformance -------------------------------------------------- #
def test_adapter_satisfies_protocol():
    from adapters.base import BrokerAdapter

    a = _adapter(FakeClient())
    assert isinstance(a, BrokerAdapter)
    assert a.name == "Tiger"

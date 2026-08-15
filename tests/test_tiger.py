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
    def __init__(self, stock_orders=None, option_orders=None, positions=None,
                 fund_df=None, funding_df=None, mleg_orders=None,
                 asset_task_orders=None):
        self._stock_orders = stock_orders or []
        self._option_orders = option_orders or []
        self._mleg_orders = mleg_orders or []
        # Corporate-action settlements: returned by get_filled_orders but NOT by
        # get_transactions (they have no transaction). Routed by sec_type below.
        self._asset_task = asset_task_orders or []
        self._positions = positions or {}
        self._fund_df = fund_df
        self._funding_df = funding_df

    def get_managed_accounts(self):
        return []  # adapter falls back to [None] (the default account)

    @staticmethod
    def _is_option(o):
        return getattr(getattr(o, "contract", None), "put_call", None) is not None

    def get_filled_orders(self, sec_type=None, **kw):
        st = str(sec_type)
        if st.endswith("STK"):
            return self._stock_orders + [o for o in self._asset_task if not self._is_option(o)]
        if st.endswith("MLEG"):
            return self._mleg_orders
        return self._option_orders + [o for o in self._asset_task if self._is_option(o)]

    def _all_orders(self):
        return self._stock_orders + self._option_orders + self._mleg_orders + self._asset_task

    @staticmethod
    def _oid(o):
        return getattr(o, "id", None) or getattr(o, "order_id", None)

    def get_transactions(self, account=None, sec_type=None, start_time=None,
                         end_time=None, limit=100, page_token=None, **kw):
        """Fill-time discovery: one fill stub per order carrying its order_id.
        Combos surface under OPT (as at the real endpoint), so OPT discovery
        includes the multi-leg orders too."""
        st = str(sec_type)
        if st.endswith("STK"):
            src = self._stock_orders
        elif st.endswith("OPT"):
            src = self._option_orders + self._mleg_orders
        else:
            src = []
        result = [SimpleNamespace(order_id=self._oid(o)) for o in src]
        # page_token passed -> return the response object (adapter reads .result).
        return SimpleNamespace(result=result, next_page_token=None)

    def get_order(self, account=None, id=None, show_charges=None, **kw):
        for o in self._all_orders():
            if self._oid(o) == id:
                return o
        return None

    def get_positions(self, sec_type=None, **kw):
        key = "OPT" if str(sec_type).endswith("OPT") else "STK"
        return self._positions.get(key, [])

    def get_fund_details(self, seg_types=None, start_date=None, **kw):
        return self._fund_df

    def get_funding_history(self, seg_type=None, **kw):
        return self._funding_df


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


def test_resting_order_missed_by_bulk_is_recovered_by_fill_discovery():
    """The resting-order bug: an order placed before the placement-scan window but
    FILLED inside the window. Bulk get_filled_orders (placement-time) misses it;
    fill-time get_transactions discovers it and get_order recovers its detail."""
    resting = _order(id=7777, action="SELL", filled=28, avg_fill_price=54.0,
                     contract=_contract(symbol="SNDK"))

    class BulkMissesResting(FakeClient):
        def get_filled_orders(self, sec_type=None, **kw):
            return []  # placement scan doesn't reach the old resting order

    a = _adapter(BulkMissesResting(stock_orders=[resting]))
    (t,) = a.fetch_stock_executions(since=None)
    assert t.ticker == "SNDK"
    assert t.qty == Decimal("28")
    assert t.action is StockAction.SELL
    assert t.dedup_key == "Tiger:7777"


def test_asset_task_settlement_included_without_transaction():
    """Option exercise/assignment (and share call-away): Tiger books a filled
    order tagged source='asset-task' but produces NO transaction, so fill-time
    discovery misses it. It must still be captured, else an expired ITM option
    never closes and reconciles as 'missing from broker' forever."""
    settle = _order(
        id=8801, action="BUY", filled=2, avg_fill_price=0.0, source="asset-task",
        contract=_contract(symbol="SNDK", put_call="CALL", strike=1400.0,
                           expiry="20260814", multiplier=100),
    )
    a = _adapter(FakeClient(asset_task_orders=[settle]))
    (t,) = a.fetch_option_executions(since=None)
    assert t.underlying == "SNDK"
    assert t.strike == Decimal("1400")
    assert t.option_type is OptionType.CALL
    assert t.action is OptionAction.BUY
    assert t.qty == Decimal("2")
    assert t.premium == Decimal("0")
    assert t.dedup_key == "Tiger:8801"


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


def _leg(**kw):
    base = dict(action="BUY", filled_quantity=1, total_quantity=1, symbol="NBIS",
                put_call="PUT", strike="175.0", expiry="20260821",
                avg_filled_price=0.96, currency="USD", multiplier=100)
    base.update(kw)
    return SimpleNamespace(**base)


def test_mleg_combo_decomposed_into_legs():
    # A vertical: sell the 175 put, buy the 180 put — one MLEG order, two legs
    # each with its own action/strike/price. Must become two OptionTrades.
    mleg = _order(
        id=9001, contract=_contract(symbol="NBIS", strike=None, put_call=None),
        contract_legs=[
            _leg(action="SELL", strike="175.0", avg_filled_price=0.96),
            _leg(action="BUY", strike="180.0", avg_filled_price=1.30),
        ],
    )
    a = _adapter(FakeClient(mleg_orders=[mleg]))
    trades = sorted(a.fetch_option_executions(since=None), key=lambda t: t.strike)

    assert len(trades) == 2
    short, long_ = trades  # 175 then 180
    assert short.underlying == "NBIS" and short.strike == Decimal("175.0")
    assert short.option_type is OptionType.PUT
    assert short.action is OptionAction.SELL
    assert short.premium == Decimal("0.96")
    assert long_.strike == Decimal("180.0") and long_.action is OptionAction.BUY
    # order fee lands on the first leg only (not multiplied across legs)
    assert short.fee == Decimal("1.5") and long_.fee == Decimal("0")
    # distinct, stable dedup keys per leg
    assert short.dedup_key == "Tiger:9001:0" and long_.dedup_key == "Tiger:9001:1"


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
    by_type = {m.type for m in moves}
    # Deposits/withdrawals are owned by get_funding_history; fund_details must NOT
    # emit external capital (its classification of it is unreliable).
    assert CashType.DEPOSIT not in by_type
    assert CashType.WITHDRAWAL not in by_type
    # Non-external types are still returned: the dividend and the unmapped row.
    assert CashType.DIVIDEND in by_type
    assert len(moves) == 2
    # unknown type kept, classified out of external capital, tagged in note
    mystery = next(m for m in moves if "unmapped" in m.note)
    assert mystery.type is CashType.INTERNAL_TRANSFER
    assert not mystery.type.is_external_capital


def test_cash_movements_funding_history_deposits_withdrawals():
    """Deposits/withdrawals come from get_funding_history (§8), a different
    endpoint than get_fund_details (which carries fees/dividends only)."""
    funding = pd.DataFrame(
        [
            {"id": 100, "currency": "SGD", "amount": 5000.0, "type_desc": "Deposit", "created_at": _ms(2026, 2, 1)},
            {"id": 101, "currency": "USD", "amount": 200.0, "type_desc": "Withdraw", "created_at": _ms(2026, 2, 5)},
            {"id": 102, "currency": "USD", "amount": 9.0, "type_desc": "Interest", "created_at": _ms(2026, 2, 6)},
        ]
    )
    a = _adapter(FakeClient(funding_df=funding))
    moves = a.fetch_cash_movements(since=None)
    # Only deposit + withdraw are mapped; the unrelated row is ignored.
    assert len(moves) == 2
    dep = next(m for m in moves if m.type is CashType.DEPOSIT)
    wd = next(m for m in moves if m.type is CashType.WITHDRAWAL)
    assert dep.amount == Decimal("5000.0") and dep.currency == "SGD"
    assert dep.date == date(2026, 2, 1)
    assert wd.amount == Decimal("200.0")  # stored positive


def test_cash_movements_funding_history_failure_is_swallowed():
    """A broken/absent funding endpoint must not abort the whole cash fetch."""
    class NoFunding(FakeClient):
        def get_funding_history(self, seg_type=None, **kw):
            raise RuntimeError("no funding access on this account")

    df = pd.DataFrame(
        [{"id": 1, "currency": "SGD", "amount": 12.5, "fund_type": "CASH_DIVIDEND", "settled_time": _ms(2026, 1, 4)}]
    )
    a = _adapter(NoFunding(fund_df=df))
    moves = a.fetch_cash_movements(since=None)  # must not raise
    assert len(moves) == 1 and moves[0].type is CashType.DIVIDEND


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

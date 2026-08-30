"""Tests for Longbridge adapter (step 6).

Mocks the Longbridge SDK to ensure the adapter maps fields correctly to the common schema.
"""

from __future__ import annotations

import sys
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from longport.openapi import OrderSide, OrderStatus, CashFlowDirection
from adapters.longbridge import LongbridgeAdapter, LongbridgeCredentials
from adapters.base import (
    Broker, StockAction, CashType, AssetType, OptionAction, OptionType,
)


# --- SDK Fakes ---

class FakeExecution:
    """Fill-level record from history_executions (the discovery source)."""
    def __init__(self, order_id, trade_done_at):
        self.order_id = order_id
        self.trade_done_at = trade_done_at


class FakeOrderDetail:
    """order_detail return: full order fields + charge_detail (fee)."""
    def __init__(self, order_id, symbol, side, executed_price, executed_quantity,
                 updated_at, total_amount, currency="USD", price=None):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.executed_price = executed_price
        self.executed_quantity = executed_quantity
        self.updated_at = updated_at
        self.currency = currency
        self.price = price
        self.charge_detail = MagicMock()
        self.charge_detail.total_amount = total_amount


class FakePosition:
    def __init__(self, symbol, quantity, cost_price, currency="USD"):
        self.symbol = symbol
        self.quantity = quantity
        self.cost_price = cost_price
        self.currency = currency


class FakePositionChannel:
    def __init__(self, positions):
        self.account_channel = "lb"
        self.positions = positions


class FakePositionsResponse:
    """Mirrors longport's StockPositionsResponse (positions grouped by channel)."""
    def __init__(self, positions):
        self.channels = [FakePositionChannel(positions)]


class FakeCashFlow:
    def __init__(self, balance, business_time, transaction_flow_name, description, business_type=1, currency="USD"):
        self.balance = balance
        self.business_time = business_time
        self.transaction_flow_name = transaction_flow_name
        self.description = description
        self.business_type = business_type
        self.currency = currency


class TestLongbridgeAdapter(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.adapter = LongbridgeAdapter(client=self.mock_client, timezone="UTC")

    def test_fetch_stock_executions(self):
        dt = datetime(2025, 3, 15, 10, 0, tzinfo=timezone.utc)
        self.mock_client.history_executions.return_value = [
            FakeExecution("o1", dt),
            FakeExecution("o2", dt),
        ]

        def mock_order_detail(order_id):
            if order_id == "o1":
                return FakeOrderDetail("o1", "AAPL.US", OrderSide.Buy, "150.00", "10", dt, "1.50")
            return FakeOrderDetail("o2", "TSLA.US", OrderSide.Sell, "200.00", "5", dt, "2.00")

        self.mock_client.order_detail.side_effect = mock_order_detail

        trades = self.adapter.fetch_stock_executions(date(2025, 3, 1))

        self.assertEqual(len(trades), 2)
        
        t1 = trades[0]
        self.assertEqual(t1.broker, Broker.LONGBRIDGE)
        self.assertEqual(t1.ticker, "AAPL")
        self.assertEqual(t1.action, StockAction.BUY)
        self.assertEqual(t1.qty, Decimal("10"))
        self.assertEqual(t1.price, Decimal("150.00"))
        self.assertEqual(t1.fee, Decimal("1.50"))
        self.assertEqual(t1.fill_id, "o1")
        self.assertEqual(t1.date, date(2025, 3, 15))

        t2 = trades[1]
        self.assertEqual(t2.action, StockAction.SELL)
        self.assertEqual(t2.fee, Decimal("2.00"))
        self.assertEqual(t2.ticker, "TSLA")

    def test_option_orders_route_to_options_not_stocks(self):
        # Option executions arrive via history_executions with an OCC-style symbol.
        # fetch_stock_executions must skip them; fetch_option_executions claims them.
        dt = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
        self.mock_client.history_executions.return_value = [
            FakeExecution("s1", dt),
            FakeExecution("o1", dt),
        ]

        def mock_order_detail(order_id):
            if order_id == "s1":
                return FakeOrderDetail("s1", "AAPL.US", OrderSide.Buy, "150.00", "10", dt, "0.10")
            return FakeOrderDetail("o1", "PYPL260828C60000.US", OrderSide.Buy, "0.98", "1", dt, "0.10")

        self.mock_client.order_detail.side_effect = mock_order_detail

        stocks = self.adapter.fetch_stock_executions(date(2026, 8, 10))
        self.assertEqual([t.ticker for t in stocks], ["AAPL"])  # option excluded

        opts = self.adapter.fetch_option_executions(date(2026, 8, 10))
        self.assertEqual(len(opts), 1)
        o = opts[0]
        self.assertEqual(o.broker, Broker.LONGBRIDGE)
        self.assertEqual(o.underlying, "PYPL")
        self.assertEqual(o.option_type, OptionType.CALL)
        self.assertEqual(o.strike, Decimal("60"))
        self.assertEqual(o.expiry, date(2026, 8, 28))
        self.assertEqual(o.action, OptionAction.BUY)
        self.assertEqual(o.qty, Decimal("1"))
        self.assertEqual(o.premium, Decimal("0.98"))
        self.assertEqual(o.fee, Decimal("0.10"))
        self.assertEqual(o.fill_id, "o1")

    def test_fetch_positions(self):
        self.mock_client.stock_positions.return_value = FakePositionsResponse([
            FakePosition("MSFT.US", "50", "300.00"),
        ])

        positions = self.adapter.fetch_positions()
        
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p.broker, Broker.LONGBRIDGE)
        self.assertEqual(p.asset_type, AssetType.STOCK)
        self.assertEqual(p.symbol, "MSFT")
        self.assertEqual(p.qty, Decimal("50"))
        self.assertEqual(p.avg_cost, Decimal("300.00"))

    def test_fetch_cash_movements(self):
        dt = datetime(2025, 3, 15, 10, 0, tzinfo=timezone.utc)
        self.mock_client.cash_flow.return_value = [
            FakeCashFlow("1000.00", dt, "Deposit", "ACH Transfer"),
            FakeCashFlow("-5.00", dt, "Fee", "Commission"),
        ]

        movements = self.adapter.fetch_cash_movements(None)

        self.assertEqual(len(movements), 2)
        m1 = movements[0]
        self.assertEqual(m1.type, CashType.DEPOSIT)
        self.assertEqual(m1.amount, Decimal("1000.00"))
        m2 = movements[1]
        self.assertEqual(m2.type, CashType.FEE)
        self.assertEqual(m2.amount, Decimal("5.00"))

    def test_fetch_multi_leg_option_executions(self):
        """Test that multi-leg combo orders (e.g. vertical spread) decompose into per-leg OptionTrades."""
        dt = datetime(2025, 3, 15, 10, 0, tzinfo=timezone.utc)
        self.mock_client.history_executions.return_value = [
            FakeExecution("combo1", dt),
        ]
        self.mock_client.order_detail.return_value = FakeOrderDetail(
            "combo1", "SNDQ260821P23/25.US", OrderSide.Buy, "1.50", "2", dt, "2.00"
        )

        trades = self.adapter.fetch_option_executions(date(2025, 3, 1))

        self.assertEqual(len(trades), 2)
        # Leg 0: SNDQ 23 Put (Lower strike -> Sell for Put credit spread)
        # Leg 1: SNDQ 25 Put (Higher strike -> Buy for Put credit spread)
        self.assertEqual(trades[0].underlying, "SNDQ")
        self.assertEqual(trades[0].strike, Decimal("23"))
        self.assertEqual(trades[0].option_type, OptionType.PUT)
        self.assertEqual(trades[0].action, OptionAction.SELL)
        self.assertEqual(trades[0].fill_id, "combo1:0")
        self.assertEqual(trades[0].fee, Decimal("2.00"))  # fee on first leg

        self.assertEqual(trades[1].underlying, "SNDQ")
        self.assertEqual(trades[1].strike, Decimal("25"))
        self.assertEqual(trades[1].option_type, OptionType.PUT)
        self.assertEqual(trades[1].action, OptionAction.BUY)
        self.assertEqual(trades[1].fill_id, "combo1:1")
        self.assertEqual(trades[1].fee, Decimal("0"))

    def test_fetch_multi_leg_option_positions(self):
        """Test that multi-leg combo positions decompose into per-leg Positions."""
        self.mock_client.stock_positions.return_value = FakePositionsResponse([
            FakePosition("SNDQ260821P23/25.US", "2", "1.50"),
        ])

        positions = self.adapter.fetch_positions()
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0].symbol, "SNDQ")
        self.assertEqual(positions[0].strike, Decimal("23"))
        self.assertEqual(positions[1].symbol, "SNDQ")
        self.assertEqual(positions[1].strike, Decimal("25"))

    # -- zero-fee option-misreport guard ----------------------------------- #
    # Longbridge sometimes reports an option/combo close under the bare underlying
    # symbol with an empty charge_detail — indistinguishable from a stock fill by
    # symbol or fees. The price sanity-check (premium << share price) catches it.

    def _one_fill(self, oid, symbol, side, price, qty, total_amount):
        dt = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
        self.mock_client.history_executions.return_value = [FakeExecution(oid, dt)]
        self.mock_client.order_detail.return_value = FakeOrderDetail(
            oid, symbol, side, price, qty, dt, total_amount)

    def test_zero_fee_low_price_fill_dropped_as_misreported_option(self):
        # MRVL "1 share" @ $4.89 with no fees, spot ~$217 → option premium, drop it.
        self._one_fill("m1", "MRVL.US", OrderSide.Buy, "4.89", "1", "0.00")
        with patch.object(LongbridgeAdapter, "_live_price", return_value=Decimal("216.62")):
            stocks = self.adapter.fetch_stock_executions(date(2026, 8, 25))
        self.assertEqual(stocks, [])

    def test_zero_fee_normal_price_fill_kept_as_stock(self):
        # Real zero-fee stock: CRWV 100 @ $86.31, spot ~$84 → keep it.
        self._one_fill("c1", "CRWV.US", OrderSide.Sell, "86.31", "100", "0.00")
        with patch.object(LongbridgeAdapter, "_live_price", return_value=Decimal("84.23")):
            stocks = self.adapter.fetch_stock_executions(date(2026, 8, 25))
        self.assertEqual([t.ticker for t in stocks], ["CRWV"])
        self.assertEqual(stocks[0].price, Decimal("86.31"))

    def test_price_check_failsafe_keeps_fill_when_quote_unavailable(self):
        # A quote failure must never drop a fill (better a stray row than lost data).
        self._one_fill("m1", "MRVL.US", OrderSide.Buy, "4.89", "1", "0.00")
        with patch.object(LongbridgeAdapter, "_live_price", return_value=None):
            stocks = self.adapter.fetch_stock_executions(date(2026, 8, 25))
        self.assertEqual([t.ticker for t in stocks], ["MRVL"])

    def test_fee_bearing_fill_is_never_price_checked(self):
        # A cheap but genuine stock that carries a fee must skip the price check
        # entirely — no quote lookup, never dropped.
        self._one_fill("s1", "SNDL.US", OrderSide.Buy, "2.50", "100", "1.00")
        with patch.object(LongbridgeAdapter, "_live_price",
                          side_effect=AssertionError("must not fetch a quote")):
            stocks = self.adapter.fetch_stock_executions(date(2026, 8, 25))
        self.assertEqual([t.ticker for t in stocks], ["SNDL"])

    def test_price_implausible_threshold_and_failsafe(self):
        with patch.object(LongbridgeAdapter, "_live_price", return_value=Decimal("100")):
            self.assertTrue(self.adapter._price_implausible_for_stock("X", Decimal("39")))
            self.assertFalse(self.adapter._price_implausible_for_stock("X", Decimal("41")))
        with patch.object(LongbridgeAdapter, "_live_price", return_value=None):
            self.assertFalse(self.adapter._price_implausible_for_stock("X", Decimal("1")))
        self.assertFalse(self.adapter._price_implausible_for_stock("X", Decimal("0")))

if __name__ == "__main__":
    unittest.main()


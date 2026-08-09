"""Tests for Longbridge adapter (step 6).

Mocks the Longbridge SDK to ensure the adapter maps fields correctly to the common schema.
"""

from __future__ import annotations

import sys
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from longport.openapi import OrderSide, OrderStatus, CashFlowDirection
from adapters.longbridge import LongbridgeAdapter, LongbridgeCredentials
from adapters.base import Broker, StockAction, CashType, AssetType


# --- SDK Fakes ---

class FakeOrder:
    def __init__(self, order_id, symbol, side, executed_price, executed_quantity, updated_at, currency="USD"):
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.executed_price = executed_price
        self.executed_quantity = executed_quantity
        self.updated_at = updated_at
        self.currency = currency
        self.status = OrderStatus.Filled


class FakeOrderDetail:
    def __init__(self, total_amount):
        self.charge_detail = MagicMock()
        self.charge_detail.total_amount = total_amount


class FakePosition:
    def __init__(self, symbol, quantity, cost_price, currency="USD"):
        self.symbol = symbol
        self.quantity = quantity
        self.cost_price = cost_price
        self.currency = currency


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
        self.mock_client.history_orders.return_value = [
            FakeOrder("o1", "AAPL.US", OrderSide.Buy, "150.00", "10", dt),
            FakeOrder("o2", "TSLA.US", OrderSide.Sell, "200.00", "5", dt),
        ]
        
        def mock_order_detail(order_id):
            if order_id == "o1":
                return FakeOrderDetail("1.50")
            return FakeOrderDetail("2.00")
            
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

    def test_fetch_positions(self):
        self.mock_client.stock_positions.return_value = [
            FakePosition("MSFT.US", "50", "300.00"),
        ]

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

if __name__ == "__main__":
    unittest.main()

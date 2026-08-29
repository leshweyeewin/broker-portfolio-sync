"""Tests for core/reconcile.py — Seeding and reconciliation (§5, §9)."""

from __future__ import annotations

import sys
import os
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.reconcile import seed_positions, reconcile, expire_worthless_options
from core.fifo_pl import Holding
from adapters.base import (
    AssetType,
    Broker,
    OptionType,
    Position,
    StockAction,
    OptionAction,
)

class TestReconcile(unittest.TestCase):

    def test_seed_positions_stock(self):
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.STOCK,
                symbol="AAPL",
                qty=Decimal("100"),
                avg_cost=Decimal("150.00"),
                currency="USD",
                as_of=date(2025, 1, 1),
            )
        ]
        
        stocks, options = seed_positions(positions, date(2025, 3, 14))
        
        self.assertEqual(len(stocks), 1)
        self.assertEqual(len(options), 0)
        
        s = stocks[0]
        self.assertEqual(s.action, StockAction.OPENING_BALANCE)
        self.assertEqual(s.qty, Decimal("100"))
        self.assertEqual(s.price, Decimal("150.00"))
        self.assertEqual(s.total, Decimal("-15000.00"))  # Acquisition is negative cash flow
        self.assertEqual(s.dedup_key, "Tiger:opening:AAPL")

    def test_seed_positions_short_option(self):
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.OPTION,
                symbol="TSLA",
                qty=Decimal("-2"),
                avg_cost=Decimal("5.00"),
                currency="USD",
                as_of=date(2025, 1, 1),
                option_type=OptionType.PUT,
                strike=Decimal("200"),
                expiry=date(2025, 4, 18),
                multiplier=Decimal("100"),
            )
        ]
        
        stocks, options = seed_positions(positions, date(2025, 3, 14))
        
        self.assertEqual(len(stocks), 0)
        self.assertEqual(len(options), 1)
        
        o = options[0]
        self.assertEqual(o.action, OptionAction.OPENING_BALANCE)
        self.assertEqual(o.qty, Decimal("-2"))
        self.assertEqual(o.premium, Decimal("5.00"))
        # Short option total: premium * qty * mult = 5 * (-2) * 100 = -1000
        # Acquisition=True for OPENING_BALANCE, so _signed_total(True, -1000) = +1000
        self.assertEqual(o.total, Decimal("1000.00"))
        self.assertEqual(o.dedup_key, "Tiger:opening:TSLA:Put:200:2025-04-18")

    def test_reconcile_match(self):
        holdings = [
            Holding(
                broker=Broker.TIGER,
                instrument="AAPL",
                symbol="AAPL",
                qty=Decimal("100"),
                avg_price=Decimal("150"),
                open_fees=Decimal("0"),
                currency="USD",
                multiplier=Decimal("1"),
            )
        ]
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.STOCK,
                symbol="AAPL",
                qty=Decimal("100"),
                avg_cost=Decimal("150"),
                currency="USD",
                as_of=date.today(),
            )
        ]
        
        warnings = reconcile(holdings, positions)
        self.assertEqual(len(warnings), 0)

    def test_reconcile_mismatch(self):
        holdings = [
            Holding(
                broker=Broker.TIGER,
                instrument="AAPL",
                symbol="AAPL",
                qty=Decimal("100"),
                avg_price=Decimal("150"),
                open_fees=Decimal("0"),
                currency="USD",
                multiplier=Decimal("1"),
            )
        ]
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.STOCK,
                symbol="AAPL",
                qty=Decimal("150"), # Broker reports 150 (maybe missed fill or split)
                avg_cost=Decimal("150"),
                currency="USD",
                as_of=date.today(),
            )
        ]
        
        warnings = reconcile(holdings, positions)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Qty mismatch", warnings[0])
        self.assertIn("AAPL", warnings[0])

    def test_reconcile_missing_from_pipeline(self):
        holdings = []
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.STOCK,
                symbol="TSLA",
                qty=Decimal("50"),
                avg_cost=Decimal("200"),
                currency="USD",
                as_of=date.today(),
            )
        ]
        
        warnings = reconcile(holdings, positions)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Missing from pipeline", warnings[0])
        self.assertIn("TSLA", warnings[0])

    def test_reconcile_option_match(self):
        # Regression: option keys must match despite Holding.instrument being a
        # formatted display string ("SPY 2026-03-20 400 Put") while the broker
        # side uses the raw underlying symbol ("SPY"). Strike 400 vs 400.0 too.
        holdings = [
            Holding(
                broker=Broker.TIGER,
                instrument="SPY 2026-03-20 400 Put",
                symbol="SPY",
                qty=Decimal("-2"),
                avg_price=Decimal("5"),
                open_fees=Decimal("0"),
                currency="USD",
                multiplier=Decimal("100"),
                option_type=OptionType.PUT,
                strike=Decimal("400"),
                expiry=date(2026, 3, 20),
            )
        ]
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.OPTION,
                symbol="SPY",
                qty=Decimal("-2"),
                avg_cost=Decimal("5"),
                currency="USD",
                option_type=OptionType.PUT,
                strike=Decimal("400.0"),  # different Decimal repr — must still match
                expiry=date(2026, 3, 20),
                multiplier=Decimal("100"),
            )
        ]

        warnings = reconcile(holdings, positions)
        self.assertEqual(warnings, [])

    def test_reconcile_option_qty_mismatch(self):
        holdings = [
            Holding(
                broker=Broker.TIGER,
                instrument="SPY 2026-03-20 400 Put",
                symbol="SPY",
                qty=Decimal("-2"),
                avg_price=Decimal("5"),
                open_fees=Decimal("0"),
                currency="USD",
                multiplier=Decimal("100"),
                option_type=OptionType.PUT,
                strike=Decimal("400"),
                expiry=date(2026, 3, 20),
            )
        ]
        positions = [
            Position(
                broker=Broker.TIGER,
                asset_type=AssetType.OPTION,
                symbol="SPY",
                qty=Decimal("-1"),  # broker shows one closed
                avg_cost=Decimal("5"),
                currency="USD",
                option_type=OptionType.PUT,
                strike=Decimal("400"),
                expiry=date(2026, 3, 20),
                multiplier=Decimal("100"),
            )
        ]

        warnings = reconcile(holdings, positions)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Qty mismatch", warnings[0])
        self.assertIn("SPY", warnings[0])

    def test_find_stale_open_rows_detects_expired_and_closed_positions(self):
        from tools.reconcile_fixup import find_stale_open_rows
        from sheets.writer import PortfolioWriter, build_stock_row, build_option_row
        from tests.test_writer import FakeSheetClient
        from adapters.base import StockTrade, OptionTrade, StockAction, OptionAction, OptionType, Broker

        class DummyAdapter:
            name = "Tiger"
            def fetch_positions(self):
                # Broker holds no open positions
                return []

        client = FakeSheetClient()
        writer = PortfolioWriter(client)
        writer.ensure_tabs()

        stk = StockTrade(
            date=date(2026, 3, 14),
            broker=Broker.TIGER,
            ticker="AAPL",
            action=StockAction.BUY,
            qty=Decimal("10"),
            price=Decimal("150"),
            fee=Decimal("1.5"),
            currency="USD",
            fill_id="stk1",
        )
        writer.upsert_stocks([build_stock_row(stk, status="Open")])

        # Expired option
        opt = OptionTrade(
            date=date(2026, 1, 1),
            broker=Broker.TIGER,
            underlying="SHOP",
            option_type=OptionType.PUT,
            strike=Decimal("130"),
            qty=Decimal("-1"),
            expiry=date(2026, 1, 1),
            action=OptionAction.OPENING_BALANCE,
            premium=Decimal("3.5"),
            fee=Decimal("0"),
            currency="USD",
            fill_id="opt1",
        )
        writer.upsert_options([build_option_row(opt, status="Open")])

        stock_fixups, option_fixups = find_stale_open_rows(writer, [DummyAdapter()], today=date(2026, 8, 14))
        self.assertEqual(len(stock_fixups), 1)
        self.assertEqual(stock_fixups[0]["instrument"], "AAPL")

        self.assertEqual(len(option_fixups), 1)
        self.assertTrue(option_fixups[0]["is_expired"])

    # --- worthless-expiry close-out --------------------------------------- #
    def _opt_holding(self, **kw):
        base = dict(
            broker=Broker.TIGER, instrument="x", symbol="AAPL",
            qty=Decimal("-2"), avg_price=Decimal("1.50"), open_fees=Decimal("0"),
            currency="USD", multiplier=Decimal("100"),
            option_type=OptionType.CALL, strike=Decimal("200"),
            expiry=date(2026, 8, 14),
        )
        base.update(kw)
        return Holding(**base)

    def test_expire_worthless_closes_expired_missing_option(self):
        # Short 2x call, expired yesterday, broker no longer reports it -> close
        # at premium 0 (worthless), flattening the short and realizing the credit.
        h = self._opt_holding()
        closes = expire_worthless_options([h], positions=[], today=date(2026, 8, 15))
        self.assertEqual(len(closes), 1)
        c = closes[0]
        self.assertEqual(c.underlying, "AAPL")
        self.assertEqual(c.action, OptionAction.BUY)  # flatten the short
        self.assertEqual(c.qty, Decimal("2"))
        self.assertEqual(c.premium, Decimal("0"))
        self.assertEqual(c.date, date(2026, 8, 14))   # realized on expiry
        self.assertEqual(c.expiry, date(2026, 8, 14))
        self.assertEqual(c.dedup_key, "Tiger:expiry:AAPL:Call:200:2026-08-14")

    def test_expire_worthless_long_option_sells_to_close(self):
        h = self._opt_holding(qty=Decimal("3"))  # long
        (c,) = expire_worthless_options([h], positions=[], today=date(2026, 8, 15))
        self.assertEqual(c.action, OptionAction.SELL)  # flatten the long
        self.assertEqual(c.qty, Decimal("3"))
        self.assertEqual(c.premium, Decimal("0"))

    def test_expire_worthless_skips_not_yet_expired(self):
        h = self._opt_holding(expiry=date(2026, 9, 18))  # future
        self.assertEqual(expire_worthless_options([h], [], date(2026, 8, 15)), [])

    def test_expire_worthless_skips_when_broker_still_holds(self):
        # Expired on the calendar but the broker still reports it -> not expired
        # away yet; leave it (don't fabricate a close).
        h = self._opt_holding()
        pos = [Position(
            broker=Broker.TIGER, asset_type=AssetType.OPTION, symbol="AAPL",
            qty=Decimal("-2"), avg_cost=Decimal("1.50"), currency="USD",
            option_type=OptionType.CALL, strike=Decimal("200"),
            expiry=date(2026, 8, 14), multiplier=Decimal("100"),
        )]
        self.assertEqual(expire_worthless_options([h], pos, date(2026, 8, 15)), [])


if __name__ == "__main__":
    unittest.main()


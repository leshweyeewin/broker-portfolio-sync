"""Tests for run.py — the orchestrator, fully offline with fakes.

Covers:
- happy path: rows written, Run Log appended, status OK, no alert
- realized P/L is joined onto closing rows and FX-converted to SGD
- only external deposits/withdrawals reach the Transactions tab
- one broker failing -> PARTIAL status, its leg dropped, alert fired
- all brokers failing -> FAILED
- reconciliation mismatch -> surfaced in warnings + alert, run still OK-status
- seeding synthesizes Opening Balance rows fed into FIFO
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from adapters.base import (
    AssetType,
    Broker,
    CashMovement,
    CashType,
    OptionAction,
    OptionType,
    Position,
    StockAction,
    StockTrade,
)
from sheets.writer import (
    STOCKS_HEADERS,
    TRANSACTIONS_HEADERS,
    UpsertResult,
)
import run as run_module
from run import run_sync


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeAdapter:
    def __init__(self, name, *, stocks=None, options=None, cash=None, positions=None, raises=None):
        self.name = name
        self._stocks = stocks or []
        self._options = options or []
        self._cash = cash or []
        self._positions = positions or []
        self._raises = raises

    def fetch_stock_executions(self, since):
        if self._raises:
            raise self._raises
        return self._stocks

    def fetch_option_executions(self, since):
        return self._options

    def fetch_cash_movements(self, since):
        return self._cash

    def fetch_positions(self):
        return self._positions


class FakeWriter:
    """Records what the orchestrator asked to be written."""

    def __init__(self):
        self.ensured = False
        self.stock_rows: list[list[Any]] = []
        self.option_rows: list[list[Any]] = []
        self.txn_rows: list[list[Any]] = []
        self.run_log: list[list[Any]] = []
        self.dashboard: list[list[Any]] = []

    def ensure_tabs(self):
        self.ensured = True

    def upsert_stocks(self, rows):
        self.stock_rows = rows
        return UpsertResult(tab="Stocks", added=len(rows), updated=0)

    def upsert_options(self, rows):
        self.option_rows = rows
        return UpsertResult(tab="Options", added=len(rows), updated=0)

    def upsert_transactions(self, rows):
        self.txn_rows = rows
        return UpsertResult(tab="Transactions", added=len(rows), updated=0)

    def append_run_log(self, row):
        self.run_log.append(row)

    def overwrite_dashboard(self, blocks):
        self.dashboard = blocks


class FakeFx:
    """Deterministic FX: USD->SGD * 1.35, everything else identity."""

    def to_sgd(self, amount, currency, on):
        if currency.upper() == "USD":
            return amount * Decimal("1.35")
        return amount

    def cached_pairs_for_date(self, on):
        return {"USDSGD": Decimal("1.35")}


class RecordingNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, text: str) -> bool:
        self.messages.append(text)
        return True


TODAY = date(2026, 8, 9)


def _col(row, headers, name):
    return row[headers.index(name)]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_happy_path_writes_and_logs_ok():
    trade = StockTrade(
        date=date(2026, 1, 2), broker=Broker.TIGER, ticker="AAPL",
        action=StockAction.BUY, qty=10, price="150", fee="1", currency="USD",
    )
    position = Position(
        broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="AAPL",
        qty=10, avg_cost="150", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[trade], positions=[position])
    writer = FakeWriter()
    notifier = RecordingNotifier()

    result = run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=notifier)

    assert result.status == "OK"
    assert result.reconciliation == "OK"
    assert writer.ensured is True
    assert len(writer.stock_rows) == 1
    assert result.stocks_added == 1
    assert len(writer.run_log) == 1
    assert notifier.messages == []  # clean run => no alert
    # Buy is still open -> no realized P/L cell
    assert _col(writer.stock_rows[0], STOCKS_HEADERS, "Status") == "Open"


def test_realized_pl_joined_and_converted():
    buy = StockTrade(
        date=date(2026, 1, 2), broker=Broker.TIGER, ticker="AAPL",
        action=StockAction.BUY, qty=10, price="100", fee="0", currency="USD",
    )
    sell = StockTrade(
        date=date(2026, 2, 2), broker=Broker.TIGER, ticker="AAPL",
        action=StockAction.SELL, qty=10, price="120", fee="0", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[buy, sell], positions=[])
    writer = FakeWriter()

    run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=RecordingNotifier())

    sell_row = next(
        r for r in writer.stock_rows
        if _col(r, STOCKS_HEADERS, "Action") == "Sell"
    )
    assert _col(sell_row, STOCKS_HEADERS, "Status") == "Closed"
    # Native realized: (120-100)*10 = 200
    assert _col(sell_row, STOCKS_HEADERS, "Realized P/L") == 200.0
    # SGD: 200 * 1.35 = 270
    assert _col(sell_row, STOCKS_HEADERS, "Realized P/L (SGD)") == 270.0


def test_only_external_cash_reaches_transactions():
    deposit = CashMovement(
        date=date(2026, 1, 1), broker=Broker.TIGER, type=CashType.DEPOSIT,
        amount="1000", currency="USD",
    )
    dividend = CashMovement(
        date=date(2026, 1, 5), broker=Broker.TIGER, type=CashType.DIVIDEND,
        amount="5", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, cash=[deposit, dividend], positions=[])
    writer = FakeWriter()

    run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=RecordingNotifier())

    assert len(writer.txn_rows) == 1
    assert _col(writer.txn_rows[0], TRANSACTIONS_HEADERS, "Type") == "Deposit"
    # 1000 USD * 1.35
    assert _col(writer.txn_rows[0], TRANSACTIONS_HEADERS, "Amount (SGD)") == 1350.0


def test_one_broker_failure_is_partial_and_alerts():
    good = FakeAdapter(
        Broker.TIGER.value,
        stocks=[StockTrade(
            date=date(2026, 1, 2), broker=Broker.TIGER, ticker="AAPL",
            action=StockAction.BUY, qty=1, price="10", currency="USD",
        )],
        positions=[],
    )
    bad = FakeAdapter(Broker.MOOMOO.value, raises=RuntimeError("gateway down"))
    writer = FakeWriter()
    notifier = RecordingNotifier()

    result = run_sync([good, bad], writer, FakeFx(), today=TODAY, notifier=notifier)

    assert result.status == "PARTIAL"
    assert len(writer.stock_rows) == 1  # good broker still synced
    assert any("gateway down" in w for w in result.warnings)
    assert len(notifier.messages) == 1
    assert "PARTIAL" in notifier.messages[0]


def test_all_brokers_failing_is_failed():
    bad = FakeAdapter(Broker.TIGER.value, raises=RuntimeError("boom"))
    writer = FakeWriter()
    notifier = RecordingNotifier()

    result = run_sync([bad], writer, FakeFx(), today=TODAY, notifier=notifier)

    assert result.status == "FAILED"
    assert len(notifier.messages) == 1


def test_reconciliation_mismatch_surfaces_and_alerts():
    # Pipeline computes 10 shares from the buy; broker reports 15 -> mismatch.
    buy = StockTrade(
        date=date(2026, 1, 2), broker=Broker.TIGER, ticker="AAPL",
        action=StockAction.BUY, qty=10, price="100", currency="USD",
    )
    position = Position(
        broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="AAPL",
        qty=15, avg_cost="100", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[buy], positions=[position])
    writer = FakeWriter()
    notifier = RecordingNotifier()

    result = run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=notifier)

    assert result.status == "OK"  # fetch succeeded; data just disagrees
    assert "mismatch" in result.reconciliation
    assert any("Qty mismatch" in w for w in result.warnings)
    assert len(notifier.messages) == 1


def test_seeding_creates_opening_balance_rows():
    position = Position(
        broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="TSLA",
        qty=50, avg_cost="200", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, positions=[position])
    writer = FakeWriter()

    result = run_sync(
        [adapter], writer, FakeFx(), today=TODAY, seed=True, notifier=RecordingNotifier()
    )

    assert len(writer.stock_rows) == 1
    assert _col(writer.stock_rows[0], STOCKS_HEADERS, "Action") == "Opening Balance"
    # Seed matches the reported position -> reconciliation clean.
    assert result.reconciliation == "OK"

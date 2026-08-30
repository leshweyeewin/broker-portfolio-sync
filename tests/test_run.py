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
    OptionTrade,
    OptionType,
    Position,
    StockAction,
    StockTrade,
)
from sheets.writer import (
    OPTIONS_HEADERS,
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

    def __init__(self, opening_stocks=None, opening_options=None):
        self.ensured = False
        self.stock_rows: list[list[Any]] = []
        self.option_rows: list[list[Any]] = []
        self.txn_rows: list[list[Any]] = []
        self.run_log: list[list[Any]] = []
        self.dashboard: list[list[Any]] = []
        self._opening_stocks = opening_stocks or []
        self._opening_options = opening_options or []

    def ensure_tabs(self):
        self.ensured = True

    def apply_formatting(self):
        pass

    def sort_data_tabs(self):
        pass

    def read_opening_balances(self):
        return list(self._opening_stocks), list(self._opening_options)

    def read_net_capital_in_by_broker(self):
        return dict(getattr(self, "_net_capital_in", {}))

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


class DateAwareFx:
    """FX that differs by date, so we can tell which date drove the conversion."""

    def __init__(self, rates):
        self._rates = rates  # {date: Decimal}

    def to_sgd(self, amount, currency, on):
        return amount * self._rates[on]

    def cached_pairs_for_date(self, on):
        return {"USDSGD": self._rates.get(on, Decimal("1"))}


def test_worthless_expiry_pl_converted_at_opening_date_rate():
    # Short put opened 18 Aug, expiring 28 Aug, that the broker no longer reports
    # (expired/assigned) -> synthesized worthless close. Its realized premium P/L
    # must be FX-converted at the 18 Aug (premium) rate, not the 28 Aug rate.
    short_put = OptionTrade(
        date=date(2026, 8, 18), broker=Broker.TIGER, underlying="CRWV",
        option_type=OptionType.PUT, strike="100", qty=1, expiry=date(2026, 8, 28),
        action=OptionAction.SELL, premium="2.68", fee="0", currency="USD",
    )
    adapter = FakeAdapter(Broker.TIGER.value, options=[short_put], positions=[])
    writer = FakeWriter()
    fx = DateAwareFx({date(2026, 8, 18): Decimal("1.2764"),
                      date(2026, 8, 28): Decimal("1.3000")})

    run_sync([adapter], writer, fx, today=date(2026, 8, 30),
             notifier=RecordingNotifier())

    # The realized P/L lands on the closing row — here the synthesized worthless
    # buy-to-close, dated on the expiry (28 Aug).
    close_row = next(
        r for r in writer.option_rows
        if _col(r, OPTIONS_HEADERS, "Action") == "Buy"
    )
    assert _col(close_row, OPTIONS_HEADERS, "Status") == "Closed"
    assert _col(close_row, OPTIONS_HEADERS, "Expiry") == "2026-08-28"
    # Native premium kept: 2.68 * 100 = 268
    assert _col(close_row, OPTIONS_HEADERS, "P/L") == 268.0
    # SGD at the 18 Aug rate (1.2764), NOT 28 Aug (1.30): 268 * 1.2764 = 342.0752
    assert _col(close_row, OPTIONS_HEADERS, "P/L (SGD)") == 342.0752


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


def test_collect_broker_data_times_out_on_hang():
    """A broker that blocks (no timeout of its own — e.g. a wedged OpenD) is
    abandoned after timeout_s and reported as an error, never hangs the run."""
    import time
    from run import collect_broker_data

    class HangingAdapter(FakeAdapter):
        def fetch_stock_executions(self, since):
            time.sleep(30)  # would block the whole run without the timeout guard
            return []

    data = collect_broker_data(HangingAdapter(Broker.MOOMOO.value), since=None, timeout_s=0.2)
    assert data.error is not None
    assert "timed out" in data.error
    assert data.stocks == [] and data.positions == []


def test_collect_broker_data_closes_adapter():
    """The orchestrator calls close() so broker connections aren't leaked."""
    from run import collect_broker_data

    class ClosableAdapter(FakeAdapter):
        closed = False
        def close(self):
            self.closed = True

    a = ClosableAdapter(Broker.MOOMOO.value)
    collect_broker_data(a, since=None)
    assert a.closed is True


# --------------------------------------------------------------------------- #
# Seed persistence (§5/§14): forward runs load persisted opening balances
# --------------------------------------------------------------------------- #
def test_forward_run_loads_opening_balances_and_reconciles():
    """A non-seed run must load persisted opening balances into FIFO, else it
    would flag every long-held position as missing from the pipeline."""
    ob = StockTrade(date=date(2026, 8, 1), broker=Broker.TIGER, ticker="AAPL",
                    action=StockAction.OPENING_BALANCE, qty=10, price="150", currency="USD")
    position = Position(broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="AAPL",
                        qty=10, avg_cost="150", currency="USD")
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[], positions=[position])
    writer = FakeWriter(opening_stocks=[ob])
    notifier = RecordingNotifier()

    result = run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=notifier)

    assert result.reconciliation == "OK"  # opening balance -> holding matches broker
    assert notifier.messages == []


def test_forward_run_sell_closes_seeded_position_with_pl():
    """A new sell against a seeded position computes realized P/L off the seed
    cost basis and leaves holdings flat."""
    ob = StockTrade(date=date(2026, 8, 1), broker=Broker.TIGER, ticker="AAPL",
                    action=StockAction.OPENING_BALANCE, qty=10, price="100", currency="USD")
    sell = StockTrade(date=date(2026, 8, 5), broker=Broker.TIGER, ticker="AAPL",
                      action=StockAction.SELL, qty=10, price="120", currency="USD")
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[sell], positions=[])  # broker now flat
    writer = FakeWriter(opening_stocks=[ob])

    result = run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=RecordingNotifier())

    assert result.reconciliation == "OK"
    sell_row = next(r for r in writer.stock_rows if _col(r, STOCKS_HEADERS, "Action") == "Sell")
    assert _col(sell_row, STOCKS_HEADERS, "Status") == "Closed"
    assert _col(sell_row, STOCKS_HEADERS, "Realized P/L") == 200.0  # (120-100)*10


def test_forward_run_drops_fills_on_or_before_seed():
    """Fills dated on/before the seed are already baked into the opening balance,
    so they must be dropped to avoid double-counting."""
    ob = StockTrade(date=date(2026, 8, 1), broker=Broker.TIGER, ticker="AAPL",
                    action=StockAction.OPENING_BALANCE, qty=10, price="100", currency="USD")
    pre = StockTrade(date=date(2026, 7, 20), broker=Broker.TIGER, ticker="AAPL",
                     action=StockAction.BUY, qty=10, price="90", currency="USD")
    position = Position(broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="AAPL",
                        qty=10, avg_cost="100", currency="USD")
    adapter = FakeAdapter(Broker.TIGER.value, stocks=[pre], positions=[position])
    writer = FakeWriter(opening_stocks=[ob])

    result = run_sync([adapter], writer, FakeFx(), today=TODAY, notifier=RecordingNotifier())

    assert result.reconciliation == "OK"  # 10 (opening), not 20 (opening + dropped buy)
    actions = {_col(r, STOCKS_HEADERS, "Action") for r in writer.stock_rows}
    assert actions == {"Opening Balance"}  # the pre-seed buy was dropped


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


# --------------------------------------------------------------------------- #
# Seed back-out: opening balance = current position − forward-journal net
# --------------------------------------------------------------------------- #
def test_backout_openings_reconciles_including_closed_instrument():
    """opening + Σ(journal net) must reconstruct the current position for every
    instrument — including one fully closed during the window (no position row)."""
    from core.reconcile import seed_positions
    from run import _backout_openings, _stock_instrument

    seed_date = date(2026, 8, 9)
    positions = [
        Position(broker=Broker.TIGER, asset_type=AssetType.STOCK, symbol="AAPL",
                 qty=Decimal("100"), avg_cost=Decimal("200"), currency="USD"),
    ]
    journal = [
        # partial sell of a still-held pre-seed lot
        StockTrade(date=date(2026, 8, 10), broker=Broker.TIGER, ticker="AAPL",
                   action=StockAction.SELL, qty=40, price="210", currency="USD", fill_id="a1"),
        # a lot fully closed in-window: not in current positions
        StockTrade(date=date(2026, 8, 11), broker=Broker.TIGER, ticker="CEG",
                   action=StockAction.SELL, qty=50, price="9", currency="USD", fill_id="c1"),
    ]

    ob_stocks, _ = seed_positions(positions, seed_date)
    openings = _backout_openings(ob_stocks, journal, seed_date, _stock_instrument)
    by_ticker = {o.ticker: o for o in openings}

    # AAPL: held 100 now, sold 40 in-window -> opened the window with 140.
    assert by_ticker["AAPL"].qty == Decimal("140")
    # CEG: flat now, sold 50 in-window -> opened the window long 50 (synthesized).
    assert by_ticker["CEG"].qty == Decimal("50")
    assert by_ticker["CEG"].action == StockAction.OPENING_BALANCE
    assert by_ticker["CEG"].price == Decimal("9")  # journal VWAP
    # all openings dated the seed date, one row per instrument
    assert all(o.date == seed_date for o in openings)

    # Reconcile property: opening + net journal == current position.
    def net(ticker):
        return sum((t.qty if t.action.is_acquisition else -t.qty)
                   for t in journal if t.ticker == ticker)
    assert by_ticker["AAPL"].qty + net("AAPL") == Decimal("100")
    assert by_ticker["CEG"].qty + net("CEG") == Decimal("0")


# --------------------------------------------------------------------------- #
# Dashboard: Net Capital In / Account Value / Total P/L
# --------------------------------------------------------------------------- #
def test_dashboard_total_pl_is_value_minus_capital():
    from run import _build_dashboard
    dash = _build_dashboard(
        "OK", [], "OK",
        account_value_sgd={"Tiger": Decimal("150"), "Longbridge": Decimal("60"),
                           "MooMoo": Decimal("80")},
        net_capital_in={"Tiger": Decimal("100"), "Longbridge": Decimal("50")},
    )
    rows = {r[0]: r for r in dash}
    # header order: [Metric, Longbridge, Tiger, MooMoo, Total (SGD)]
    cap = rows["Net Capital In (SGD)"]
    val = rows["Account Value (SGD)"]
    pl = rows["Total P/L (SGD)"]
    assert cap[1] == 50.0 and cap[2] == 100.0            # Longbridge, Tiger
    assert val[2] == 150.0 and val[3] == 80.0            # Tiger, MooMoo
    # Total P/L = value - capital: Longbridge 10, Tiger 50
    assert pl[1] == 10.0 and pl[2] == 50.0
    # MooMoo holds value but capital-in unknown -> blank, not a misleading number
    assert pl[3] == ""
    # Total column excludes the un-computable MooMoo leg
    assert pl[4] == 60.0


def test_dashboard_has_rolling_realized_rows():
    from run import _build_dashboard
    dash = _build_dashboard(
        "OK", [], "OK",
        weekly_realized_sgd_by_broker={"Tiger": Decimal("120"), "Longbridge": Decimal("30")},
        monthly_realized_sgd_by_broker={"Tiger": Decimal("300"), "Longbridge": Decimal("30")},
        ytd_realized_sgd_by_broker={"Tiger": Decimal("500"), "Longbridge": Decimal("30")},
    )
    rows = {r[0]: r for r in dash}
    wk = rows["This Week Realized (SGD)"]    # [Metric, Longbridge, Tiger, MooMoo, Total]
    assert wk[1] == 30.0 and wk[2] == 120.0 and wk[3] == 0.0
    assert wk[4] == 150.0                     # total = 150
    mo = rows["This Month Realized (SGD)"]
    assert mo[2] == 300.0 and mo[4] == 330.0
    yr = rows["This Year Realized (SGD)"]
    assert yr[2] == 500.0 and yr[4] == 530.0
    # the all-time realized row is gone — only the three rolling windows remain
    assert "Realized P/L (SGD)" not in rows

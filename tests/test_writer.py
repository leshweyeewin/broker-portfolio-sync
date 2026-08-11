"""Tests for sheets/writer.py — fully offline using FakeSheetClient.

Coverage:
- _col_letter helper
- _fmt formatting helper
- FakeSheetClient in-memory store behaves like the real API
- ensure_tabs creates missing tabs and writes headers
- ensure_tabs is idempotent (safe to call multiple times)
- upsert: new rows are appended with correct values
- upsert: re-running the same rows produces no duplicates (idempotency)
- upsert: changed values on an existing key are updated in-place
- upsert: unchanged rows are neither re-appended nor re-updated
- upsert: rows with empty dedup_key are skipped
- overwrite_dashboard clears and rewrites each run
- append_run_log always appends (never updates)
- build_transaction_row / build_stock_row / build_option_row produce correct columns
- _dedup_key is always the last column
"""

from __future__ import annotations

import copy
import sys
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from sheets.writer import (
    TRANSACTIONS_HEADERS,
    STOCKS_HEADERS,
    OPTIONS_HEADERS,
    RUN_LOG_HEADERS,
    DASHBOARD_HEADERS,
    TAB_TRANSACTIONS,
    TAB_STOCKS,
    TAB_OPTIONS,
    TAB_DASHBOARD,
    TAB_RUN_LOG,
    ALL_TABS,
    PortfolioWriter,
    UpsertResult,
    _col_letter,
    _fmt,
    build_transaction_row,
    build_stock_row,
    build_option_row,
)
from adapters.base import (
    Broker, CashType, StockAction, OptionAction, OptionType, Direction,
    CashMovement, StockTrade, OptionTrade,
)


# --------------------------------------------------------------------------- #
# FakeSheetClient — in-memory simulation of the Sheets API
# --------------------------------------------------------------------------- #

class FakeSheetClient:
    """In-memory Sheets API fake. Stores data as a dict of tab -> rows (list of lists)."""

    def __init__(self, existing_tabs: list[str] | None = None):
        # tab name -> list of rows (list of lists). Row 0 = header if present.
        self._data: dict[str, list[list[Any]]] = {}
        self._sheet_ids: dict[str, int] = {}
        self._next_id = 1
        self._hide_requests: list[dict] = []  # captured for inspection

        for tab in (existing_tabs or []):
            self._data[tab] = []
            self._sheet_ids[tab] = self._next_id
            self._next_id += 1

    # --- reads ---

    def get_values(self, range_: str, value_render_option: str = "FORMATTED_VALUE") -> list[list[Any]]:
        tab = range_.split("!")[0]
        return copy.deepcopy(self._data.get(tab, []))

    def get_sheet_metadata(self) -> dict:
        sheets = []
        for title, sid in self._sheet_ids.items():
            sheets.append({"properties": {"title": title, "sheetId": sid}})
        return {"sheets": sheets}

    # --- writes ---

    def batch_update_values(self, data: list[dict]) -> dict:
        for item in data:
            range_ = item["range"]
            values = item["values"]
            tab, cell = range_.split("!") if "!" in range_ else (range_, "A1")

            if tab not in self._data:
                self._data[tab] = []

            # Parse start row (1-based) from cell like "A1", "A5:M5"
            cell_ref = cell.split(":")[0]
            row_num = int("".join(c for c in cell_ref if c.isdigit())) - 1  # 0-based

            for i, row_vals in enumerate(values):
                target = row_num + i
                while len(self._data[tab]) <= target:
                    self._data[tab].append([])
                # Ensure row is wide enough
                cur = self._data[tab][target]
                while len(cur) < len(row_vals):
                    cur.append("")
                for j, v in enumerate(row_vals):
                    if j < len(cur):
                        cur[j] = v
                    else:
                        cur.append(v)
        return {}

    def append_values(self, range_: str, values: list[list[Any]]) -> dict:
        tab = range_.split("!")[0]
        if tab not in self._data:
            self._data[tab] = []
        for row in values:
            self._data[tab].append(list(row))
        return {}

    def batch_update(self, requests: list[dict]) -> dict:
        replies = []
        for req in requests:
            if "addSheet" in req:
                title = req["addSheet"]["properties"]["title"]
                self._data[title] = []
                self._sheet_ids[title] = self._next_id
                self._next_id += 1
                replies.append({"addSheet": {"properties": {"title": title, "sheetId": self._sheet_ids[title]}}})
            elif "updateDimensionProperties" in req:
                self._hide_requests.append(req)
                replies.append({})
        return {"replies": replies}

    def clear_range(self, range_: str) -> None:
        tab = range_.split("!")[0]
        self._data[tab] = []

    @property
    def spreadsheet_id(self) -> str:
        return "fake-spreadsheet-id"

    # --- inspection helpers ---

    def rows(self, tab: str) -> list[list[Any]]:
        return copy.deepcopy(self._data.get(tab, []))

    def data_rows(self, tab: str, header_rows: int = 1) -> list[list[Any]]:
        """Return rows after the header."""
        all_rows = self.rows(tab)
        return all_rows[header_rows:] if len(all_rows) > header_rows else []

    def tabs(self) -> set[str]:
        return set(self._sheet_ids.keys())


# --------------------------------------------------------------------------- #
# Sample dataclass factories
# --------------------------------------------------------------------------- #

def _cash_movement(key: str = "Tiger:abc123", amount: str = "1000") -> CashMovement:
    return CashMovement(
        date=date(2025, 3, 14),
        broker=Broker.TIGER,
        type=CashType.DEPOSIT,
        amount=Decimal(amount),
        currency="USD",
        note="initial deposit",
        fill_id="abc123",
    )


def _stock_trade(
    ticker: str = "AAPL",
    action: StockAction = StockAction.BUY,
    fill_id: str = "f001",
    qty: str = "10",
    price: str = "150.00",
) -> StockTrade:
    return StockTrade(
        date=date(2025, 3, 14),
        broker=Broker.TIGER,
        ticker=ticker,
        action=action,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("1.50"),
        currency="USD",
        fill_id=fill_id,
    )


def _option_trade(fill_id: str = "o001") -> OptionTrade:
    return OptionTrade(
        date=date(2025, 3, 14),
        broker=Broker.TIGER,
        underlying="AAPL",
        option_type=OptionType.PUT,
        strike=Decimal("140"),
        qty=Decimal("1"),
        expiry=date(2025, 4, 18),
        action=OptionAction.SELL,
        premium=Decimal("3.50"),
        fee=Decimal("0.65"),
        currency="USD",
        strategy="Short Put",
        direction=Direction.BULLISH,
        fill_id=fill_id,
    )


# --------------------------------------------------------------------------- #
# _col_letter tests
# --------------------------------------------------------------------------- #

def test_col_letter_single():
    assert _col_letter(1) == "A"
    assert _col_letter(26) == "Z"


def test_col_letter_double():
    assert _col_letter(27) == "AA"
    assert _col_letter(52) == "AZ"
    assert _col_letter(53) == "BA"


def test_col_letter_matches_header_widths():
    assert _col_letter(len(TRANSACTIONS_HEADERS)) == "H"
    assert _col_letter(len(STOCKS_HEADERS)) == "M"
    assert _col_letter(len(OPTIONS_HEADERS)) == "Q"  # Direction column removed


# --------------------------------------------------------------------------- #
# _fmt tests
# --------------------------------------------------------------------------- #

def test_fmt_decimal():
    assert _fmt(Decimal("1.35")) == pytest.approx(1.35)


def test_fmt_date():
    assert _fmt(date(2025, 3, 14)) == "2025-03-14"


def test_fmt_none():
    assert _fmt(None) == ""


def test_fmt_string():
    assert _fmt("hello") == "hello"


# --------------------------------------------------------------------------- #
# ensure_tabs tests
# --------------------------------------------------------------------------- #

def test_ensure_tabs_creates_all_tabs():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()
    assert set(ALL_TABS).issubset(client.tabs())


def test_ensure_tabs_writes_headers():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()
    stocks_rows = client.rows(TAB_STOCKS)
    # Stocks/Options carry a 2-row summary (rows 1-2); headers live on row 3.
    assert stocks_rows[2] == [str(h) for h in STOCKS_HEADERS]


def test_ensure_tabs_idempotent():
    """Calling ensure_tabs twice must not duplicate headers or tabs."""
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()
    writer.ensure_tabs()
    # Stocks: 2 summary rows + 1 header row; Transactions: 1 header row.
    assert len(client.rows(TAB_STOCKS)) == 3
    assert len(client.rows(TAB_TRANSACTIONS)) == 1


def test_ensure_tabs_hides_dedup_column():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()
    assert len(client._hide_requests) >= 3  # Transactions, Stocks, Options


# --------------------------------------------------------------------------- #
# upsert_transactions tests
# --------------------------------------------------------------------------- #

def _tx_rows(n: int = 1) -> list[list]:
    cm = _cash_movement()
    row = build_transaction_row(cm, Decimal("1350"))
    return [row] * n if n == 1 else [
        build_transaction_row(
            CashMovement(
                date=date(2025, 3, i + 14),
                broker=Broker.TIGER,
                type=CashType.DEPOSIT,
                amount=Decimal("1000"),
                currency="USD",
                note="dep",
                fill_id=f"dep{i}",
            ),
            Decimal("1350"),
        )
        for i in range(n)
    ]


def test_upsert_transactions_appends_new_rows():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    row = build_transaction_row(_cash_movement(), Decimal("1350"))
    result = writer.upsert_transactions([row])

    assert result.added == 1
    assert result.updated == 0
    assert len(client.data_rows(TAB_TRANSACTIONS)) == 1


def test_upsert_transactions_idempotent_no_dupes():
    """Running the same rows twice must not double-append."""
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    rows = _tx_rows(2)
    writer.upsert_transactions(rows)
    result2 = writer.upsert_transactions(rows)

    assert result2.added == 0
    assert result2.updated == 0
    # Still only 2 data rows
    assert len(client.data_rows(TAB_TRANSACTIONS)) == 2


def test_upsert_transactions_updates_changed_row():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    cm = _cash_movement(amount="1000")
    row_original = build_transaction_row(cm, Decimal("1350"))
    writer.upsert_transactions([row_original])

    # Same dedup_key, different SGD amount (e.g. FX rate corrected)
    row_updated = build_transaction_row(cm, Decimal("1360"))
    result = writer.upsert_transactions([row_updated])

    assert result.updated == 1
    assert result.added == 0
    data = client.data_rows(TAB_TRANSACTIONS)
    assert len(data) == 1
    # SGD column is index 5
    sgd_idx = TRANSACTIONS_HEADERS.index("Amount (SGD)")
    assert float(data[0][sgd_idx]) == pytest.approx(1360.0)


def test_upsert_skips_empty_dedup_key():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    row = ["2025-03-14", "Tiger", "Deposit", 1000, "USD", 1350, "note", ""]
    result = writer.upsert_transactions([row])
    assert result.added == 0
    assert len(client.data_rows(TAB_TRANSACTIONS)) == 0


# --------------------------------------------------------------------------- #
# upsert_stocks tests
# --------------------------------------------------------------------------- #

def test_upsert_stocks_new_row():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    trade = _stock_trade()
    row = build_stock_row(trade)
    result = writer.upsert_stocks([row])

    assert result.added == 1
    data = client.data_rows(TAB_STOCKS, header_rows=3)
    assert len(data) == 1
    assert data[0][STOCKS_HEADERS.index("Ticker")] == "AAPL"


def test_upsert_stocks_idempotent():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    trade = _stock_trade()
    row = build_stock_row(trade)
    writer.upsert_stocks([row])
    result2 = writer.upsert_stocks([row])

    assert result2.added == 0
    assert result2.updated == 0
    assert len(client.data_rows(TAB_STOCKS, header_rows=3)) == 1


def test_upsert_stocks_realized_pl_written():
    """Sell row with realized P/L populates the P/L columns."""
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    sell = _stock_trade(action=StockAction.SELL, fill_id="sell001")
    row = build_stock_row(sell, status="Closed",
                          realized_pl=Decimal("250.00"),
                          realized_pl_sgd=Decimal("337.50"))
    writer.upsert_stocks([row])

    data = client.data_rows(TAB_STOCKS, header_rows=3)
    pl_idx = STOCKS_HEADERS.index("Realized P/L")
    pl_sgd_idx = STOCKS_HEADERS.index("Realized P/L (SGD)")
    assert float(data[0][pl_idx]) == pytest.approx(250.0)
    assert float(data[0][pl_sgd_idx]) == pytest.approx(337.5)


def test_upsert_stocks_multiple_tickers_no_cross_contamination():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    aapl = build_stock_row(_stock_trade("AAPL", fill_id="a1"))
    msft = build_stock_row(_stock_trade("MSFT", fill_id="m1"))
    writer.upsert_stocks([aapl, msft])

    data = client.data_rows(TAB_STOCKS, header_rows=3)
    assert len(data) == 2
    tickers = {row[STOCKS_HEADERS.index("Ticker")] for row in data}
    assert tickers == {"AAPL", "MSFT"}


# --------------------------------------------------------------------------- #
# upsert_options tests
# --------------------------------------------------------------------------- #

def test_upsert_options_new_row():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    trade = _option_trade()
    row = build_option_row(trade)
    result = writer.upsert_options([row])

    assert result.added == 1
    data = client.data_rows(TAB_OPTIONS, header_rows=3)
    assert len(data) == 1
    assert data[0][OPTIONS_HEADERS.index("Stock")] == "AAPL"


def test_read_opening_balances_roundtrips_dedup_key():
    """Opening balances read back must keep their stored _dedup_key, so a forward
    run updates them in place instead of re-adding duplicates (real bug: a strike
    read back as 145.0 recomputed a different key)."""
    from adapters.base import (
        Broker, StockAction, OptionAction, OptionType, StockTrade, OptionTrade,
    )
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    ob_stock = StockTrade(date=date(2026, 8, 1), broker=Broker.TIGER, ticker="AAPL",
                          action=StockAction.OPENING_BALANCE, qty=10, price="150", currency="USD")
    ob_opt = OptionTrade(date=date(2026, 8, 1), broker=Broker.TIGER, underlying="SPY",
                         option_type=OptionType.PUT, strike="145", qty=1, expiry=date(2026, 9, 19),
                         action=OptionAction.OPENING_BALANCE, premium="2", currency="USD")
    writer.upsert_stocks([build_stock_row(ob_stock, status="Open")])
    writer.upsert_options([build_option_row(ob_opt, status="Open")])

    stocks, options = writer.read_opening_balances()

    assert len(stocks) == 1 and len(options) == 1
    assert stocks[0].dedup_key == ob_stock.dedup_key
    assert stocks[0].action is StockAction.OPENING_BALANCE
    assert stocks[0].ticker == "AAPL" and stocks[0].qty == Decimal("10")
    assert options[0].dedup_key == ob_opt.dedup_key  # stable despite 145 -> 145.0 readback
    assert options[0].option_type is OptionType.PUT and options[0].strike == Decimal("145")
    assert options[0].date == date(2026, 8, 1)


def test_upsert_options_idempotent():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    trade = _option_trade()
    row = build_option_row(trade)
    writer.upsert_options([row])
    result2 = writer.upsert_options([row])

    assert result2.added == 0
    assert result2.updated == 0
    assert len(client.data_rows(TAB_OPTIONS, header_rows=3)) == 1


# --------------------------------------------------------------------------- #
# Run Log tests
# --------------------------------------------------------------------------- #

def test_append_run_log_always_appends():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    row = ["2025-03-14T00:00:00Z", "OK", 5, 2, 3, 1, 4, "USD/SGD:1.34", "OK", ""]
    writer.append_run_log(row)
    writer.append_run_log(row)

    # Two log rows, not one (run log never dedupes)
    all_rows = client.rows(TAB_RUN_LOG)
    data_rows = [r for r in all_rows if r and r[0] != "Timestamp"]
    assert len(data_rows) == 2


# --------------------------------------------------------------------------- #
# Dashboard tests
# --------------------------------------------------------------------------- #

def test_overwrite_dashboard_replaces_content():
    client = FakeSheetClient()
    writer = PortfolioWriter(client)
    writer.ensure_tabs()

    writer.overwrite_dashboard([["Metric", "Tiger"], ["ROI", "12%"]])
    writer.overwrite_dashboard([["Metric", "Tiger"], ["ROI", "15%"]])  # updated

    rows = client.rows(TAB_DASHBOARD)
    assert rows[1][1] == "15%"


# --------------------------------------------------------------------------- #
# Row builder tests
# --------------------------------------------------------------------------- #

def test_build_transaction_row_length_and_dedup_key():
    cm = _cash_movement()
    row = build_transaction_row(cm, Decimal("1350"))
    assert len(row) == len(TRANSACTIONS_HEADERS)
    assert row[-1] == cm.dedup_key  # _dedup_key is last


def test_build_stock_row_length_and_dedup_key():
    trade = _stock_trade()
    row = build_stock_row(trade)
    assert len(row) == len(STOCKS_HEADERS)
    assert row[-1] == trade.dedup_key


def test_build_option_row_length_and_dedup_key():
    trade = _option_trade()
    row = build_option_row(trade)
    assert len(row) == len(OPTIONS_HEADERS)
    assert row[-1] == trade.dedup_key


def test_build_stock_row_buy_sign():
    """Buy total should be negative (cash outflow)."""
    trade = _stock_trade(action=StockAction.BUY, qty="10", price="150")
    row = build_stock_row(trade)
    total_idx = STOCKS_HEADERS.index("Total")
    # 10 * 150 = 1500, Buy is negative
    assert row[total_idx] == pytest.approx(-1500.0)


def test_build_stock_row_sell_sign():
    """Sell total should be positive (cash inflow)."""
    trade = _stock_trade(action=StockAction.SELL, qty="10", price="160", fill_id="s1")
    row = build_stock_row(trade)
    total_idx = STOCKS_HEADERS.index("Total")
    assert row[total_idx] == pytest.approx(1600.0)


if __name__ == "__main__":
    import pytest as pt
    pt.main([__file__, "-v"])

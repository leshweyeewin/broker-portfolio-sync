"""Unit tests for Deskpilot holdings generation and sheets/writer integration."""
from __future__ import annotations

import sys
import os
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sheets.writer import (
    HOLDINGS_HEADERS,
    TAB_HOLDINGS,
    TAB_STOCKS,
    TAB_OPTIONS,
    STOCKS_HEADERS,
    OPTIONS_HEADERS,
    DATA_HEADER_ROWS,
    PortfolioWriter,
    build_holdings,
)
from tests.test_writer import FakeSheetClient


def test_build_holdings_stocks_aggregation():
    stock_rows = [
        # Two open lots for AAPL on Tiger
        {"Date": "2026-08-20", "Broker": "Tiger", "Ticker": "AAPL", "Action": "BUY", "Qty": "10", "Price": "150.00", "Currency": "USD", "Status": "Open"},
        {"Date": "2026-08-21", "Broker": "Tiger", "Ticker": "AAPL", "Action": "BUY", "Qty": "20", "Price": "165.00", "Currency": "USD", "Status": "Open"},
        # One closed lot for AAPL (should be excluded)
        {"Date": "2026-08-15", "Broker": "Tiger", "Ticker": "AAPL", "Action": "BUY", "Qty": "5", "Price": "140.00", "Currency": "USD", "Status": "Closed"},
        # One open lot for TSLA on MooMoo
        {"Date": "2026-08-25", "Broker": "MooMoo", "Ticker": "TSLA", "Action": "BUY", "Qty": "5", "Price": "200.00", "Currency": "USD", "Status": "Open"},
    ]

    holdings = build_holdings(stock_rows, [])
    assert len(holdings) == 2

    # AAPL: 10*150 + 20*165 = 1500 + 3300 = 4800 / 30 = 160.0 avg cost
    aapl = next(r for r in holdings if r[2] == "AAPL")
    assert aapl[0] == "position"
    assert aapl[1] == "Tiger"
    assert aapl[2] == "AAPL"
    assert aapl[4] == 30
    assert aapl[5] == 160.0
    assert aapl[11] == "USD"

    # TSLA: 5 shares at 200.0
    tsla = next(r for r in holdings if r[2] == "TSLA")
    assert tsla[0] == "position"
    assert tsla[1] == "MooMoo"
    assert tsla[4] == 5
    assert tsla[5] == 200.0


def test_build_holdings_options_aggregation():
    opt_rows = [
        # Short put on NVDA 195 Put
        {"Date": "2026-08-25", "Broker": "MooMoo", "Stock": "NVDA", "Type": "PUT", "Strike": "195.0", "Qty": "-2", "Expiry": "2026-09-18", "Premium": "3.50", "Currency": "USD", "Status": "Open"},
        {"Date": "2026-08-26", "Broker": "MooMoo", "Stock": "NVDA", "Type": "PUT", "Strike": "195.0", "Qty": "-1", "Expiry": "2026-09-18", "Premium": "4.10", "Currency": "USD", "Status": "Open"},
        # Long call on AVGO 400 Call
        {"Date": "2026-08-20", "Broker": "Tiger", "Stock": "AVGO", "Type": "CALL", "Strike": "400.0", "Qty": "1", "Expiry": "2026-09-18", "Premium": "12.00", "Currency": "USD", "Status": "Open"},
        # Closed option (should be excluded)
        {"Date": "2026-08-10", "Broker": "Tiger", "Stock": "AVGO", "Type": "CALL", "Strike": "380.0", "Qty": "1", "Expiry": "2026-08-21", "Premium": "5.00", "Currency": "USD", "Status": "Closed"},
    ]

    holdings = build_holdings([], opt_rows)
    assert len(holdings) == 2

    # NVDA Short Put: total qty = -3, weighted prem = (2*3.5 + 1*4.1) / 3 = 11.1 / 3 = 3.7
    nvda = next(r for r in holdings if r[2] == "NVDA")
    assert nvda[0] == "option"
    assert nvda[1] == "MooMoo"
    assert nvda[3] == "PUT"
    assert nvda[4] == -3
    assert nvda[7] == 195.0
    assert nvda[8] == "2026-09-18"
    assert nvda[9] == "sell-to-open"
    assert nvda[10] == 3.7

    # AVGO Long Call: total qty = 1, prem = 12.0
    avgo = next(r for r in holdings if r[2] == "AVGO")
    assert avgo[0] == "option"
    assert avgo[1] == "Tiger"
    assert avgo[3] == "CALL"
    assert avgo[4] == 1
    assert avgo[7] == 400.0
    assert avgo[9] == "buy-to-open"
    assert avgo[10] == 12.0


def test_build_holdings_with_cash():
    cash = {"SGD": 12450.50, "USD": 3120.00}
    holdings = build_holdings([], [], cash=cash)
    assert len(holdings) == 2
    sgd = next(r for r in holdings if r[11] == "SGD")
    assert sgd[0] == "cash"
    assert sgd[12] == 12450.50

    usd = next(r for r in holdings if r[11] == "USD")
    assert usd[0] == "cash"
    assert usd[12] == 3120.00


def test_update_holdings_in_portfolio_writer():
    client = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS, TAB_HOLDINGS])
    writer = PortfolioWriter(client)

    # Set up Stocks and Options sheets with summary blocks and rows
    stocks_data = [
        ["Total P/L", "=SUM(K4:K)"],
        ["Total Fees", "=SUM(H4:H)"],
        [str(h) for h in STOCKS_HEADERS],
        ["2026-08-25", "MooMoo", "AAPL", "BUY", 15, 175.50, 2632.50, 1.50, "USD", "Open", "", "", "k1", "", ""],
        ["2026-08-20", "Tiger", "MSFT", "BUY", 10, 400.00, 4000.00, 1.50, "USD", "Closed", "200.00", "270.00", "k2", "", ""],
    ]
    options_data = [
        ["Total P/L", "=SUM(O4:O)"],
        ["Total Fees", "=SUM(L4:L)"],
        [str(h) for h in OPTIONS_HEADERS],
        ["2026-08-25", "Tiger", "Long Call", "CRWV", "CALL", 98.0, 2, "2026-09-18", "BUY", 5.50, -1100.00, 2.00, "USD", "Open", "", "", "k3", "", ""],
    ]
    client.batch_update_values([
        {"range": f"{TAB_STOCKS}!A1", "values": stocks_data},
        {"range": f"{TAB_OPTIONS}!A1", "values": options_data},
    ])

    rows = writer.update_holdings(cash={"USD": 5000.0})
    assert len(rows) == 3  # 1 stock, 1 option, 1 cash

    holdings_sheet = client.get_values(f"{TAB_HOLDINGS}!A1:Z100")
    assert holdings_sheet[0] == HOLDINGS_HEADERS
    assert len(holdings_sheet) == 4  # header + 3 rows

    # Verify stock row
    stock_row = holdings_sheet[1]
    assert stock_row[0] == "position"
    assert stock_row[1] == "MooMoo"
    assert stock_row[2] == "AAPL"
    assert stock_row[4] == 15
    assert stock_row[5] == 175.5

    # Verify option row
    opt_row = holdings_sheet[2]
    assert opt_row[0] == "option"
    assert opt_row[1] == "Tiger"
    assert opt_row[2] == "CRWV"
    assert opt_row[3] == "CALL"
    assert opt_row[4] == 2
    assert opt_row[7] == 98.0

    # Verify cash row
    cash_row = holdings_sheet[3]
    assert cash_row[0] == "cash"
    assert cash_row[11] == "USD"
    assert cash_row[12] == 5000.0

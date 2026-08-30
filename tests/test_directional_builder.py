import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest

sys.modules.setdefault("yfinance", MagicMock())

from analytics.options.option_chain import OptionContract, OptionQuote, OptionChainSnapshot
from analytics.options.directional_builder import (
    main,
    _get_snapshot,
    build_bull_call_spreads,
    build_bear_put_spreads,
    build_long_calls,
    build_cash_secured_puts,
    build_covered_calls,
    build_pmcc_leaps,
    build_full_pmcc,
    WheelFilters,
    WheelFiltersShortTerm,
)

TODAY = date(2026, 1, 1)
EXPIRY = date(2026, 2, 5)  # 35 DTE from TODAY
AS_OF = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)


def _quote(strike, right, bid, ask, delta, *, expiry=EXPIRY, oi=500, vol=120):
    contract = OptionContract(f"XYZ{strike}{right[0].upper()}", "XYZ", expiry, strike, right)
    return OptionQuote(
        contract, AS_OF, "broker_live", bid=bid, ask=ask, last=None,
        volume=vol, open_interest=oi, implied_volatility="0.35", delta=delta,
    )


def _snapshot(quotes, price="100"):
    return OptionChainSnapshot("XYZ", price, AS_OF, "broker_live", tuple(quotes))


def _directional_chain():
    # Long call at 105 (delta 0.50), short call at 110 for the debit spread.
    return _snapshot([
        _quote(Decimal("105"), "call", "4.0", "4.2", "0.50"),
        _quote(Decimal("110"), "call", "1.0", "1.1", "0.20"),
    ])


def _wheel_chain():
    return _snapshot([
        _quote(Decimal("90"), "put", "1.0", "1.1", "-0.25"),
        _quote(Decimal("110"), "call", "1.0", "1.1", "0.25"),
    ])


# --------------------------------------------------------------------------- #
# Builder library (moved out of strategies.py)
# --------------------------------------------------------------------------- #
def test_build_bull_call_spread():
    scan = build_bull_call_spreads(_directional_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "bull_call"
    assert c.net_debit > 0
    assert c.max_loss == c.net_debit


def test_build_long_call():
    scan = build_long_calls(_directional_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "long_call"
    assert c.net_debit > 0
    assert c.max_loss == c.net_debit


def test_dte_outside_window_is_rejected():
    scan = build_bull_call_spreads(_directional_chain(), EXPIRY, today=date(2026, 1, 30))  # 6 DTE
    assert scan.candidates == ()
    assert any("DTE" in r.detail for r in scan.rejections)


def test_wheel_filters_windows():
    assert (WheelFilters().min_dte, WheelFilters().max_dte) == (30, 45)
    assert (WheelFiltersShortTerm().min_dte, WheelFiltersShortTerm().max_dte) == (7, 14)


def test_build_cash_secured_puts():
    scan = build_cash_secured_puts(_wheel_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "short_put"
    assert c.net_debit < 0  # a credit


def test_build_covered_calls():
    scan = build_covered_calls(_wheel_chain(), EXPIRY, today=TODAY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.strategy == "short_call"
    assert c.net_debit < 0


def test_build_pmcc_leaps():
    leaps_expiry = date(2027, 2, 5)
    q = _quote(Decimal("50"), "call", "40.0", "41.0", "0.85", expiry=leaps_expiry)
    scan = build_pmcc_leaps(_snapshot([q]), leaps_expiry, today=TODAY)
    assert len(scan.candidates) == 1
    assert scan.candidates[0].strategy == "long_call"
    assert scan.candidates[0].net_debit > 0


def test_build_full_pmcc():
    leaps_expiry = date(2027, 2, 5)
    q_l = _quote(Decimal("50"), "call", "40.0", "41.0", "0.85", expiry=leaps_expiry)
    q_s = _quote(Decimal("110"), "call", "1.0", "1.1", "0.25", expiry=EXPIRY)
    scan = build_full_pmcc(_snapshot([q_l, q_s]), leaps_expiry, EXPIRY)
    assert len(scan.candidates) == 1
    c = scan.candidates[0]
    assert c.leaps_strike == Decimal("50")
    assert c.short_strike == Decimal("110")
    assert c.net_debit == Decimal("39.45")
    assert c.approx_max_profit == (Decimal("110") - Decimal("50")) - c.net_debit


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_directional_builder_main_no_tickers(capsys):
    assert main([]) == 1
    assert "No tickers provided" in capsys.readouterr().out


@patch("analytics.options.directional_builder._build_quote_client")
@patch("analytics.options.directional_builder._get_snapshot")
def test_directional_builder_main_with_tickers(mock_get_snapshot, mock_client, capsys):
    mock_snap = MagicMock()
    mock_snap.quotes = []
    mock_get_snapshot.return_value = mock_snap

    assert main(["AAPL"]) == 0
    assert "Scanning AAPL" in capsys.readouterr().out


@patch("analytics.options.directional_builder._build_quote_client", return_value=None)
@patch("analytics.options.directional_builder._get_snapshot")
def test_directional_builder_main_prints_candidate_values(mock_get_snapshot, _client, capsys):
    # Regression guard for the stripped f-strings: a snapshot WITH a valid bull-call
    # candidate must print real numbers, never a blank "Debit:  |".
    mock_get_snapshot.return_value = _directional_chain()
    with patch("analytics.options.directional_builder.date") as mock_date:
        mock_date.today.return_value = TODAY
        mock_date.fromisoformat = date.fromisoformat
        assert main(["XYZ"]) == 0
    out = capsys.readouterr().out
    assert "Spot Price: $100" in out
    assert "[Bull Call]" in out
    assert "Debit: $" in out and "Debit:  |" not in out


@patch("yfinance.Ticker")
@patch("analytics.options.directional_builder.fetch_option_chain")
def test_get_snapshot(mock_fetch_chain, mock_ticker):
    mock_tk = MagicMock()
    mock_tk.fast_info.last_price = 150.0
    mock_tk.options = ["2026-02-05", "2028-02-05"]
    mock_ticker.return_value = mock_tk

    mock_fetch_chain.return_value = [
        {"right": "call", "strike": 150, "bid": 5, "ask": 6, "open_interest": 100, "delta": 0.5}
    ]

    snap = _get_snapshot(MagicMock(), "AAPL")
    assert snap is not None
    assert snap.underlying == "AAPL"
    assert snap.underlying_price == 150.0
    assert len(snap.quotes) == 2
    assert snap.quotes[0].delta == 0.5  # delta is extracted, not dropped

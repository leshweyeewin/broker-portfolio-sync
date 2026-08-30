import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from decimal import Decimal
from datetime import date, timedelta

sys.modules.setdefault("yfinance", MagicMock())

import analytics.options.income_workspace as iw
from analytics.options.income_workspace import (
    WheelState,
    CC_Candidate,
    CSP_Candidate,
    PMCC_Candidate,
    get_dte,
    scan_csps,
    scan_covered_calls,
    scan_pmccs,
)

# An expiry ~30 DTE out so it lands inside the 14-45 DTE scan window.
EXP = (date.today() + timedelta(days=30)).isoformat()


def _yf_with_expiry(exp=EXP):
    tk = MagicMock()
    tk.options = (exp,)
    return tk

def test_get_dte():
    assert get_dte("2050-01-01") > 0

def test_wheel_state_creation():
    state = WheelState(ticker="AAPL", state="SHARES", qty=100, cost_basis=Decimal("150.0"))
    assert state.ticker == "AAPL"
    assert state.state == "SHARES"
    assert state.qty == 100

def test_cc_candidate():
    cc = CC_Candidate(
        ticker="AAPL",
        shares_held=100,
        cost_basis=Decimal("150.0"),
        current_price=160.0,
        strike=Decimal("170.0"),
        expiry="2026-10-01",
        premium=Decimal("2.5"),
        annualized_yield=0.15,
        call_away_price=Decimal("172.5")
    )
    assert cc.ticker == "AAPL"
    assert cc.annualized_yield == 0.15

def test_csp_candidate():
    csp = CSP_Candidate(
        ticker="TSLA",
        current_price=200.0,
        strike=Decimal("190.0"),
        expiry="2026-10-01",
        premium=Decimal("5.0"),
        return_on_risk=0.026,
        annualized_yield=0.3,
        effective_cost=Decimal("185.0"),
        delta=0.25
    )
    assert csp.effective_cost == Decimal("185.0")

def test_pmcc_candidate():
    pmcc = PMCC_Candidate(
        ticker="MSFT",
        long_strike=Decimal("200.0"),
        long_expiry="2028-01-01",
        short_strike=Decimal("350.0"),
        short_expiry="2026-10-01",
        premium=Decimal("3.0"),
        diagonal_risk=Decimal("50.0")
    )
    assert pmcc.diagonal_risk == Decimal("50.0")


# --------------------------------------------------------------------------- #
# Scanner logic (previously untested — only the dataclasses were covered)
# --------------------------------------------------------------------------- #
def test_scan_csps_finds_otm_put_within_cash_and_delta():
    put_chain = [{"right": "put", "strike": 190, "bid": 2.0, "ask": 2.2, "delta": 0.25}]
    with patch.object(iw, "get_current_price", return_value=200.0), \
         patch("yfinance.Ticker", return_value=_yf_with_expiry()), \
         patch.object(iw, "fetch_chains_for_expiry", return_value=put_chain):
        out = scan_csps(MagicMock(), ["TSLA"], available_cash=25000)
    assert len(out) == 1
    c = out[0]
    assert c.ticker == "TSLA"
    assert c.strike == Decimal("190")
    assert c.delta == 0.25
    assert c.effective_cost == Decimal("190") - c.premium


def test_scan_csps_skips_when_cash_insufficient():
    put_chain = [{"right": "put", "strike": 190, "bid": 2.0, "ask": 2.2, "delta": 0.25}]
    with patch.object(iw, "get_current_price", return_value=200.0), \
         patch("yfinance.Ticker", return_value=_yf_with_expiry()), \
         patch.object(iw, "fetch_chains_for_expiry", return_value=put_chain):
        # 190 strike needs $19,000 of collateral; only $5,000 available.
        out = scan_csps(MagicMock(), ["TSLA"], available_cash=5000)
    assert out == []


def test_scan_csps_skips_high_delta_puts():
    put_chain = [{"right": "put", "strike": 195, "bid": 6.0, "ask": 6.2, "delta": 0.60}]
    with patch.object(iw, "get_current_price", return_value=200.0), \
         patch("yfinance.Ticker", return_value=_yf_with_expiry()), \
         patch.object(iw, "fetch_chains_for_expiry", return_value=put_chain):
        out = scan_csps(MagicMock(), ["TSLA"], available_cash=25000)
    assert out == []  # delta 0.60 > 0.35 cutoff


def test_scan_covered_calls_only_scans_shares_state():
    call_chain = [{"right": "call", "strike": 110, "bid": 1.0, "ask": 1.2}]
    states = [
        WheelState("AAPL", "SHARES", 100, Decimal("90")),
        WheelState("MSFT", "CASH", 0, Decimal("0")),  # not shares -> ignored
    ]
    with patch.object(iw, "get_current_price", return_value=100.0), \
         patch("yfinance.Ticker", return_value=_yf_with_expiry()), \
         patch.object(iw, "fetch_chains_for_expiry", return_value=call_chain):
        out = scan_covered_calls(MagicMock(), states)
    assert {c.ticker for c in out} == {"AAPL"}
    assert out[0].call_away_price == Decimal("110") + out[0].premium


class _FakeDF:
    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        return self._records


def test_fetch_chains_falls_back_to_yfinance_without_broker():
    oc = SimpleNamespace(
        calls=_FakeDF([{"strike": 110.0, "bid": 1.0, "ask": 1.2,
                        "openInterest": 50, "impliedVolatility": 0.4}]),
        puts=_FakeDF([{"strike": 90.0, "bid": 0.8, "ask": 1.0,
                       "openInterest": 30, "impliedVolatility": 0.5}]),
    )
    fake_tk = MagicMock()
    fake_tk.option_chain.return_value = oc
    with patch("yfinance.Ticker", return_value=fake_tk):
        rows = iw.fetch_chains_for_expiry(None, "AAPL", EXP)  # client=None → yfinance path

    assert {r["right"] for r in rows} == {"call", "put"}
    call_row = next(r for r in rows if r["right"] == "call")
    assert call_row["strike"] == 110.0
    assert call_row["open_interest"] == 50
    assert "delta" not in call_row  # yfinance carries no live Greeks


def test_fetch_chains_prefers_broker_when_present():
    broker = MagicMock()
    broker.get_option_chain.return_value = [{"right": "put", "strike": 190, "bid": 2.0, "ask": 2.2}]
    with patch("yfinance.Ticker", side_effect=AssertionError("must not hit yfinance")):
        rows = iw.fetch_chains_for_expiry(broker, "TSLA", EXP)
    assert rows == [{"right": "put", "strike": 190, "bid": 2.0, "ask": 2.2}]


def test_scan_pmccs_uses_note_for_long_leg():
    call_chain = [{"right": "call", "strike": 210, "bid": 3.0, "ask": 3.2}]
    states = [WheelState("NVDA", "PMCC_BASE", 100, Decimal("40"), note="Long Call 150 exp 2028-01-01")]
    with patch.object(iw, "get_current_price", return_value=200.0), \
         patch("yfinance.Ticker", return_value=_yf_with_expiry()), \
         patch.object(iw, "fetch_chains_for_expiry", return_value=call_chain):
        out = scan_pmccs(MagicMock(), states)
    assert len(out) == 1
    c = out[0]
    assert c.long_strike == Decimal("150")
    assert c.long_expiry == "2028-01-01"
    assert c.short_strike == Decimal("210")

import pytest
from decimal import Decimal
from datetime import date
from analytics.income_workspace import (
    WheelState,
    CC_Candidate,
    CSP_Candidate,
    PMCC_Candidate,
    get_dte
)

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

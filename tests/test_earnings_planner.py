from datetime import date
from analytics.iv_crush import IVCrushCandidate
from analytics.earnings_planner import build_earnings_plan_row

def test_build_earnings_plan_row():
    cand = IVCrushCandidate(
        ticker="NVDA",
        earnings_date=date(2026, 9, 15),
        days_left=5,
        price=120.0,
        implied_move_pct=8.5,
        em_lower=110.0,
        em_upper=130.0,
        hist_move_pct=7.2,
        hist_bias="6↓/2↑",
        edge="RICH",
        bias="Bullish",
        strategy="Put Credit Spread (sell put below lower EM)",
        current_iv=0.45,
        iv_percentile=85.5
    )
    
    row = build_earnings_plan_row(cand)
    assert row[0] == "NVDA"
    assert row[1] == "2026-09-15"
    assert row[2] == 5
    assert row[3] == "SELL — rich premium"
    assert row[4] == 8.5
    assert row[5] == 7.2
    assert row[6] == "Bullish"
    assert row[7] == "Put Credit Spread (sell put below lower EM)"
    assert row[8] == 110.0
    assert row[9] == 130.0
    assert row[10] == 85.5

def test_build_earnings_plan_row_empty():
    cand = IVCrushCandidate(
        ticker="CRWD",
        earnings_date=date(2026, 9, 15),
        days_left=5
    )
    row = build_earnings_plan_row(cand)
    assert row[0] == "CRWD"
    assert row[4] == "" # implied move
    assert row[5] == "" # hist move
    assert row[8] == "" # lower bounds
    assert row[10] == "" # ivp

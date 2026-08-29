"""Offline unit tests for ``analytics.earnings.iv_crush`` pure helpers.

Cover the trend-bias classifier, Expected-Move bounds, strategy mapping, and the
candidate line/sort — no yfinance / network.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from analytics.earnings.iv_crush import (
    IVCrushCandidate,
    compute_iv_percentile,
    em_bounds,
    playbook_grade,
    scan_iv_crush,
    trend_bias,
    _STRATEGY_BY_BIAS,
)
from analytics.earnings.iv_crush_history import CrushStudy


# --------------------------------------------------------------------------- #
# trend_bias
# --------------------------------------------------------------------------- #

def test_trend_bias_bullish_rising_stack():
    # Steadily rising series: price > SMA20 > SMA50.
    closes = [float(i) for i in range(1, 61)]
    assert trend_bias(closes) == "Bullish"


def test_trend_bias_bearish_falling_stack():
    closes = [float(i) for i in range(60, 0, -1)]
    assert trend_bias(closes) == "Bearish"


def test_trend_bias_neutral_when_choppy():
    closes = [100.0, 101.0, 99.0] * 20  # oscillating, SMAs ~ equal
    assert trend_bias(closes) == "Neutral"


def test_trend_bias_neutral_when_too_few_points():
    assert trend_bias([1.0, 2.0, 3.0]) == "Neutral"
    assert trend_bias([]) == "Neutral"


# --------------------------------------------------------------------------- #
# em_bounds
# --------------------------------------------------------------------------- #

def test_em_bounds_symmetric():
    lo, hi = em_bounds(200.0, 5.0)  # ±5% of 200 = ±10
    assert (lo, hi) == (190.0, 210.0)


def test_em_bounds_small_move():
    lo, hi = em_bounds(50.0, 2.0)  # ±1.0
    assert (lo, hi) == (49.0, 51.0)


# --------------------------------------------------------------------------- #
# strategy mapping + candidate rendering
# --------------------------------------------------------------------------- #

def test_strategy_by_bias_covers_all():
    assert set(_STRATEGY_BY_BIAS) == {"Bullish", "Bearish", "Neutral"}
    assert "Put Credit Spread" in _STRATEGY_BY_BIAS["Bullish"]
    assert "Call Credit Spread" in _STRATEGY_BY_BIAS["Bearish"]
    assert "Iron Condor" in _STRATEGY_BY_BIAS["Neutral"]


def test_candidate_signal_from_edge():
    c = IVCrushCandidate(ticker="CCL", earnings_date=date(2026, 3, 27), days_left=3,
                         edge="RICH")
    assert "SELL" in c.signal
    c.edge = "CHEAP"
    assert "SKIP" in c.signal
    c.edge = None
    assert "NO DATA" in c.signal


def test_candidate_line_contains_key_fields():
    c = IVCrushCandidate(
        ticker="NKE", earnings_date=date(2026, 3, 31), days_left=2,
        price=53.44, implied_move_pct=9.05, em_lower=48.6, em_upper=58.3,
        hist_move_pct=7.18, hist_bias="5↓/3↑", edge="RICH",
        bias="Bearish", strategy=_STRATEGY_BY_BIAS["Bearish"],
    )
    line = c.line()
    assert "NKE" in line
    assert "Call Credit Spread" in line
    assert "EM $48.60–$58.30" in line
    assert "±9.1% vs hist ±7.2%" in line


# --------------------------------------------------------------------------- #
# scan_iv_crush with no upcoming earnings (offline, no network hit)
# --------------------------------------------------------------------------- #

def test_scan_returns_empty_when_no_earnings(monkeypatch):
    # No events -> function returns before touching yfinance.
    monkeypatch.setattr("analytics.earnings.iv_crush.get_upcoming_earnings", lambda *a, **k: [])
    assert scan_iv_crush(["NVDA"], today=date(2026, 8, 29)) == []


# --------------------------------------------------------------------------- #
# compute_iv_percentile (Step 1)
# --------------------------------------------------------------------------- #

def test_iv_percentile_fraction_below():
    hist = {"2026-08-01": 0.2, "2026-08-02": 0.3, "2026-08-03": 0.9}
    ivp = compute_iv_percentile(0.6, hist, today=date(2026, 8, 4))
    assert ivp == pytest.approx(66.67, abs=0.1)  # 2 of 3 below 0.6


def test_iv_percentile_none_when_insufficient():
    assert compute_iv_percentile(0.5, {}) is None
    assert compute_iv_percentile(0.5, {"2026-08-01": 0.4}, today=date(2026, 8, 2)) is None


def test_iv_percentile_ignores_stale_window():
    hist = {"2024-01-01": 0.9, "2026-08-01": 0.2, "2026-08-02": 0.3}
    # The 2024 point is outside the 365d window, so only the two 2026 points count.
    ivp = compute_iv_percentile(0.6, hist, today=date(2026, 8, 4))
    assert ivp == pytest.approx(100.0)  # both remaining points below 0.6


# --------------------------------------------------------------------------- #
# playbook_grade (Step 1-4 composition)
# --------------------------------------------------------------------------- #

def _graded(**kw) -> IVCrushCandidate:
    base = dict(ticker="X", earnings_date=date(2026, 9, 1), days_left=3)
    base.update(kw)
    return IVCrushCandidate(**base)


def test_grade_all_green_is_focus():
    c = _graded(iv_percentile=85.0, crush_consistency=0.8,
                crush_magnitude_pct=12.0, edge="RICH")
    assert playbook_grade(c) == (4, "FOCUS")
    assert c.grade == 4 and c.verdict == "FOCUS"


def test_grade_two_green_is_skip():
    # IVP + RICH green, but crush unknown -> only 2 greens.
    c = _graded(iv_percentile=85.0, edge="RICH")
    assert playbook_grade(c) == (2, "SKIP")


def test_grade_boundaries_resolve_to_amber():
    # Exactly at the thresholds -> NOT green (spec uses >70, >=0.75, >10).
    c = _graded(iv_percentile=70.0, crush_consistency=0.74,
                crush_magnitude_pct=10.0, edge="FAIR")
    assert playbook_grade(c) == (0, "SKIP")
    c2 = _graded(iv_percentile=70.0, crush_consistency=0.75,
                 crush_magnitude_pct=10.0, edge="FAIR")
    assert playbook_grade(c2) == (1, "SKIP")  # only consistency>=0.75 flips green


def test_grade_three_green_is_watch():
    c = _graded(iv_percentile=85.0, crush_consistency=0.8,
                crush_magnitude_pct=12.0, edge="FAIR")
    assert playbook_grade(c) == (3, "WATCH")


# --------------------------------------------------------------------------- #
# scan_iv_crush wiring: IV percentile + crush populated (offline, patched)
# --------------------------------------------------------------------------- #

def test_scan_populates_ivp_and_crush(monkeypatch):
    ev = SimpleNamespace(ticker="NVDA", earnings_date=date(2026, 9, 15), days_left=5)
    monkeypatch.setattr("analytics.earnings.iv_crush.get_upcoming_earnings", lambda *a, **k: [ev])
    # Avoid all network: stub the snapshot, history study, IV history + crush.
    monkeypatch.setattr("analytics.earnings.iv_crush._fetch_snapshot",
                        lambda tk, ed: (100.0, 8.0, [float(i) for i in range(1, 61)]))
    monkeypatch.setattr("analytics.earnings.iv_crush.historical_earnings_move",
                        lambda *a, **k: SimpleNamespace(
                            avg_abs_reaction=5.0, bear_count=5, bull_count=3))
    monkeypatch.setattr("analytics.earnings.iv_crush._load_iv_history",
                        lambda t: {"2026-09-01": 0.2, "2026-09-10": 0.3})
    monkeypatch.setattr("analytics.earnings.iv_logger.fetch_atm_iv", lambda t: 0.6)
    monkeypatch.setattr("analytics.earnings.iv_crush.historical_iv_crush",
                        lambda *a, **k: CrushStudy(
                            n=3, consistency=0.67, avg_crush_pct=12.0, events=[]))

    cands = scan_iv_crush(["NVDA"], today=date(2026, 9, 10))
    assert len(cands) == 1
    c = cands[0]
    assert c.edge == "RICH"                       # implied 8.0 vs hist 5.0
    assert c.iv_percentile == pytest.approx(100.0)
    assert c.crush_magnitude_pct == 12.0
    assert c.crush_consistency == 0.67
    line = c.line()
    assert "IVP 100%" in line
    assert "crush 12.0%×67%" in line


# --------------------------------------------------------------------------- #
# Slice 3: Wiring credit spreads
# --------------------------------------------------------------------------- #

def test_scan_wires_credit_spreads(monkeypatch):
    from analytics.options.strategies import SpreadScan, CreditSpreadCandidate
    from analytics.options.payoff import OptionLeg
    from decimal import Decimal
    
    ev = SimpleNamespace(ticker="NVDA", earnings_date=date(2026, 9, 15), days_left=5)
    monkeypatch.setattr("analytics.earnings.iv_crush.get_upcoming_earnings", lambda *a, **k: [ev])
    
    # Force a Bullish trend and RICH edge so it builds a Put Credit Spread
    monkeypatch.setattr("analytics.earnings.iv_crush._fetch_snapshot",
                        lambda tk, ed: (100.0, 8.0, [float(i) for i in range(1, 61)]))  # 1..60 is Bullish
    
    monkeypatch.setattr("analytics.earnings.iv_crush.historical_earnings_move",
                        lambda *a, **k: SimpleNamespace(
                            avg_abs_reaction=5.0, bear_count=5, bull_count=3))
                            
    class DummyYF:
        options = ("2026-09-18",)
    monkeypatch.setattr("yfinance.Ticker", lambda t: DummyYF())
    
    monkeypatch.setattr("analytics.earnings.iv_crush._build_quote_client", lambda: None)
    monkeypatch.setattr("analytics.earnings.iv_crush.fetch_option_chain", lambda *a, **k: [
        {"strike": 90, "type": "put", "bid": 1.0, "ask": 1.2},
        {"strike": 85, "type": "put", "bid": 0.5, "ask": 0.7},
    ])
    
    def dummy_builder(snap, exp, min_credit):
        return SimpleNamespace(candidates=[
            SimpleNamespace(
                legs=[
                    OptionLeg(right="put", side="sell", strike=Decimal("90"), premium=Decimal("1.0")),
                    OptionLeg(right="put", side="buy", strike=Decimal("85"), premium=Decimal("0.7")),
                ],
                net_credit=Decimal("0.30")
            )
        ])
        
    monkeypatch.setattr("analytics.earnings.iv_crush.build_put_credit_spreads", dummy_builder)
    
    cands = scan_iv_crush(["NVDA"], today=date(2026, 9, 10))
    assert len(cands) == 1
    c = cands[0]
    
    assert c.edge == "RICH"
    assert c.bias == "Bullish"
    assert "[Live: 2026-09-18 90/85 | Cr: $0.30]" in c.strategy


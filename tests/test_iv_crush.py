"""Offline unit tests for ``analytics.iv_crush`` pure helpers.

Cover the trend-bias classifier, Expected-Move bounds, strategy mapping, and the
candidate line/sort — no yfinance / network.
"""

from __future__ import annotations

from datetime import date

from analytics.iv_crush import (
    IVCrushCandidate,
    em_bounds,
    scan_iv_crush,
    trend_bias,
    _STRATEGY_BY_BIAS,
)


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
    monkeypatch.setattr("analytics.iv_crush.get_upcoming_earnings", lambda *a, **k: [])
    assert scan_iv_crush(["NVDA"], today=date(2026, 8, 29)) == []

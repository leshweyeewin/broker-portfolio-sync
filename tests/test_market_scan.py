"""Unit tests for the plugin-free volatility signals in ``analytics.market_scan``.

These cover the pure helpers only (no yfinance / network): realized-vol,
the ATM-mid picker, and the straddle-implied expected-move conversion.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from analytics.market_scan import (
    UpcomingEarnings,
    _annualized_realized_vol,
    _atm_mid,
    _expected_move_pct,
)
from analytics.screener import ScreenerResult


# --------------------------------------------------------------------------- #
# Realized volatility
# --------------------------------------------------------------------------- #

def test_realized_vol_none_when_too_few_points():
    assert _annualized_realized_vol([]) is None
    assert _annualized_realized_vol([100, 101]) is None


def test_realized_vol_flat_series_is_zero():
    assert _annualized_realized_vol([100, 100, 100, 100]) == 0.0


def test_realized_vol_scales_with_swings():
    calm = _annualized_realized_vol([100, 101, 100, 101, 100, 101])
    wild = _annualized_realized_vol([100, 130, 100, 130, 100, 130])
    assert calm is not None and wild is not None
    assert wild > calm > 0


def test_realized_vol_known_value():
    # 100->110->100->110 has a hand-checkable annualized vol (~1.75).
    vol = _annualized_realized_vol([100, 110, 100, 110])
    assert vol is not None
    assert 1.6 < vol < 1.9


def test_realized_vol_ignores_bad_points():
    # None and non-positive values are dropped, not crashed on.
    assert _annualized_realized_vol([100, None, 0, 101, 102, 103]) is not None


# --------------------------------------------------------------------------- #
# Expected move (%)
# --------------------------------------------------------------------------- #

def test_expected_move_pct_basic():
    assert _expected_move_pct(5.0, 100.0) == 5.0
    assert _expected_move_pct(2.5, 50.0) == 5.0


def test_expected_move_pct_invalid():
    assert _expected_move_pct(5.0, 0.0) is None
    assert _expected_move_pct(0.0, 100.0) is None


# --------------------------------------------------------------------------- #
# ATM mid picker
# --------------------------------------------------------------------------- #

def test_atm_mid_picks_closest_strike():
    rows = [
        {"strike": 95, "bid": 1.0, "ask": 1.2, "lastPrice": 1.1},
        {"strike": 100, "bid": 2.0, "ask": 2.2, "lastPrice": 2.1},
        {"strike": 105, "bid": 0.5, "ask": 0.7, "lastPrice": 0.6},
    ]
    assert _atm_mid(rows, 101) == 2.1  # (2.0 + 2.2) / 2


def test_atm_mid_falls_back_to_last_and_skips_unusable():
    rows = [
        {"strike": 100, "bid": 0, "ask": 0, "lastPrice": 3.0},  # no book -> lastPrice
        {"strike": 101, "bid": 0, "ask": 0, "lastPrice": 0},    # unusable -> skipped
    ]
    assert _atm_mid(rows, 100.4) == 3.0


def test_atm_mid_empty():
    assert _atm_mid([], 100) is None


# --------------------------------------------------------------------------- #
# Dataclass defaults (new fields default to "unknown")
# --------------------------------------------------------------------------- #

def test_screener_result_iv_rv_ratio_defaults_none():
    r = ScreenerResult(
        symbol="NVDA", expiry="2026-09-19", option_type="Put",
        strike=Decimal("100"), bid=Decimal("1"), ask=Decimal("1.1"),
        spread=Decimal("0.1"), delta=0.12, iv=0.4, ivp=0.0, open_interest=800,
    )
    assert r.iv_rv_ratio is None


def test_upcoming_earnings_expected_move_defaults_none():
    e = UpcomingEarnings(ticker="NVDA", earnings_date=date(2026, 2, 26), days_left=5)
    assert e.expected_move_pct is None

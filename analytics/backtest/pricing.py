"""Black-Scholes helpers for modelled option backtests (no skew, no dividends)."""

from __future__ import annotations

from math import erf, exp, log, sqrt
from statistics import NormalDist, pstdev
from typing import Sequence

from analytics.backtest.bars import Bar

_N = NormalDist()
RISK_FREE = 0.045
VOL_FLOOR = 0.15


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, is_call: bool, r: float = RISK_FREE) -> float:
    """Option value per share; intrinsic at/after expiry."""
    if t_years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    d1 = (log(spot / strike) + (r + 0.5 * vol * vol) * t_years) / (vol * sqrt(t_years))
    d2 = d1 - vol * sqrt(t_years)
    if is_call:
        return spot * _cdf(d1) - strike * exp(-r * t_years) * _cdf(d2)
    return strike * exp(-r * t_years) * _cdf(-d2) - spot * _cdf(-d1)


def strike_for_delta(spot: float, t_years: float, vol: float, target_abs_delta: float,
                     is_call: bool, r: float = RISK_FREE) -> float:
    """Exact strike whose |delta| equals ``target_abs_delta`` (closed form)."""
    d1 = _N.inv_cdf(target_abs_delta if is_call else 1.0 - target_abs_delta)
    return spot * exp(-d1 * vol * sqrt(t_years) + (r + 0.5 * vol * vol) * t_years)


def realized_vol(bars: Sequence[Bar], end_idx: int, window: int = 20) -> float | None:
    """Annualised close-to-close vol using closes up to and including ``end_idx``."""
    if end_idx < window:
        return None
    closes = [float(b.close) for b in bars[end_idx - window:end_idx + 1]]
    rets = [log(b / a) for a, b in zip(closes, closes[1:])]
    return max(pstdev(rets) * sqrt(252), VOL_FLOOR)

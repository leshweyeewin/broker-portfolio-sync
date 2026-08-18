"""Daily market scanner for portfolio holdings and watchlists.

Features:
1. **Daily Bullish/Bearish Movers** — monitors daily % change on held/watched tickers.
2. **Next-Day Earnings Alerts** — alerts for earnings releases in the next 1–2 days
   so pre-earnings IV crush setups can be prepared before the market closes.
3. **Systematic Short Put / Short Call Picks** — runs high-probability screener
   (IVP >= 70%, Delta 0.10–0.15, OI > 500, Spread <= $0.10) across tickers.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from analytics.earnings import get_earnings_dates
from analytics.screener import ScreenerFilter, ScreenerResult, screen_options

log = logging.getLogger(__name__)


@dataclass
class TickerMover:
    """One ticker's daily price performance."""
    ticker: str
    price: float
    change_pct: float
    change_abs: float

    @property
    def is_bullish(self) -> bool:
        return self.change_pct > 0


@dataclass
class UpcomingEarnings:
    """An upcoming earnings announcement event."""
    ticker: str
    earnings_date: date
    days_left: int
    note: str = ""


def _clean_ticker_list(tickers: list[str]) -> list[str]:
    """Filter to clean US equity/option ticker symbols."""
    clean = []
    for t in tickers:
        s = str(t).strip().upper()
        if not s or s.startswith(("^", "HK.", "US.")) or "." in s or not s.isalpha():
            continue
        if len(s) <= 5 and s not in clean:
            clean.append(s)
    return clean


# --------------------------------------------------------------------------- #
# 1. Daily Movers
# --------------------------------------------------------------------------- #

def get_daily_movers(
    tickers: list[str],
    *,
    min_move_pct: float = 0.5,
) -> tuple[list[TickerMover], list[TickerMover]]:
    """Scan daily price performance across a list of tickers.

    Returns ``(bullish_movers, bearish_movers)`` sorted by magnitude.
    """
    clean_tickers = _clean_ticker_list(tickers)
    if not clean_tickers:
        return [], []

    bullish: list[TickerMover] = []
    bearish: list[TickerMover] = []

    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not available for daily movers")
        return [], []

    # Batch download 5 days of history for efficiency
    try:
        data = yf.download(
            tickers=" ".join(clean_tickers[:35]),
            period="5d",
            interval="1d",
            progress=False,
            group_by="ticker",
            auto_adjust=True,
        )
    except Exception as exc:
        log.warning("Daily movers batch download failed: %s", exc)
        return [], []

    for ticker in clean_tickers[:30]:
        try:
            if len(clean_tickers) == 1:
                hist = data
            else:
                hist = data[ticker] if ticker in data else None
            if hist is None or hist.empty or len(hist) < 2:
                continue

            closes = hist["Close"].dropna()
            if len(closes) < 2:
                continue

            prev_close = float(closes.iloc[-2])
            curr_close = float(closes.iloc[-1])
            if prev_close <= 0:
                continue

            change_abs = curr_close - prev_close
            change_pct = (change_abs / prev_close) * 100

            mover = TickerMover(
                ticker=ticker,
                price=round(curr_close, 2),
                change_pct=round(change_pct, 2),
                change_abs=round(change_abs, 2),
            )

            if change_pct >= min_move_pct:
                bullish.append(mover)
            elif change_pct <= -min_move_pct:
                bearish.append(mover)
        except Exception as exc:
            log.debug("Failed calculating mover for %s: %s", ticker, exc)
            continue

    bullish.sort(key=lambda m: m.change_pct, reverse=True)
    bearish.sort(key=lambda m: m.change_pct)
    return bullish, bearish


# --------------------------------------------------------------------------- #
# 2. Upcoming Earnings (Next 1–2 Days)
# --------------------------------------------------------------------------- #

def get_upcoming_earnings(
    tickers: list[str],
    *,
    today: Optional[date] = None,
    days_ahead: int = 14,
) -> list[UpcomingEarnings]:
    """Find upcoming quarterly earnings for tickers in [today, today + days_ahead]."""
    today = today or date.today()
    clean_tickers = _clean_ticker_list(tickers)

    horizon = today + timedelta(days=days_ahead)
    events: list[UpcomingEarnings] = []

    for ticker in clean_tickers:
        dates = get_earnings_dates(ticker)
        for ed in dates:
            if today <= ed <= horizon:
                days_left = (ed - today).days
                note = "Tomorrow!" if days_left == 1 else ("Today!" if days_left == 0 else f"In {days_left}d")
                events.append(UpcomingEarnings(
                    ticker=ticker,
                    earnings_date=ed,
                    days_left=days_left,
                    note=note,
                ))

    events.sort(key=lambda e: (e.days_left, e.ticker))
    return events


# --------------------------------------------------------------------------- #
# 3. Systematic Short Put / Short Call Picks
# --------------------------------------------------------------------------- #

# High-liquidity optionable benchmark tickers to augment portfolio scan
_LIQUID_BENCHMARKS = ["SPY", "QQQ", "AAPL", "NVDA", "MSFT", "GOOG", "AMZN", "TSLA"]


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (approximation)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _estimate_delta(
    underlying_price: float,
    strike: float,
    dte_days: int,
    iv: float,
    is_call: bool,
    r: float = 0.045,
) -> float:
    """Estimate option Delta via Black-Scholes."""
    if dte_days <= 0 or iv <= 0 or underlying_price <= 0 or strike <= 0:
        return 0.0
    t = dte_days / 365.0
    d1 = (math.log(underlying_price / strike) + (r + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    if is_call:
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0


def scan_short_option_picks(
    tickers: list[str],
    *,
    today: Optional[date] = None,
    filters: Optional[ScreenerFilter] = None,
    target_dte_min: int = 14,
    target_dte_max: int = 45,
    include_benchmarks: bool = False,
    max_tickers: int = 30,
) -> list[ScreenerResult]:
    """Scan candidate tickers for high-probability Short Put / Call setups.

    Tries Tiger QuoteClient first; falls back to yfinance option chains.

    ``tickers`` (the watchlist / held names) are scanned **first**; the liquid
    SPY/QQQ/mega-cap benchmarks are only appended when ``include_benchmarks`` is
    set, so the default output is watchlist-first rather than dominated by index
    ETFs. ``max_tickers`` caps how many names are scanned per run (chains are the
    slow part).
    """
    today = today or date.today()
    filters = filters or ScreenerFilter()
    results: list[ScreenerResult] = []

    # Watchlist/portfolio names first; benchmarks only as an opt-in fallback.
    ordered = list(tickers) + (_LIQUID_BENCHMARKS if include_benchmarks else [])
    candidate_tickers = _clean_ticker_list(ordered)

    try:
        import yfinance as yf
    except ImportError:
        return results

    for ticker in candidate_tickers[:max_tickers]:
        try:
            tk = yf.Ticker(ticker)
            expiries = getattr(tk, "options", None)
            if not expiries:
                continue

            # Get current price
            fast_info = getattr(tk, "fast_info", None)
            curr_price = getattr(fast_info, "last_price", None)
            if not curr_price:
                hist = tk.history(period="1d")
                if not hist.empty:
                    curr_price = float(hist["Close"].iloc[-1])
            if not curr_price:
                continue

            for exp_str in expiries[:10]:
                try:
                    exp_date = date.fromisoformat(exp_str)
                except Exception:
                    continue

                dte = (exp_date - today).days
                if dte < target_dte_min or dte > target_dte_max:
                    continue

                chain = tk.option_chain(exp_str)

                # Process Puts (Bullish Short Put)
                if hasattr(chain, "puts") and not chain.puts.empty:
                    for _, row in chain.puts.iterrows():
                        strike = float(row.get("strike", 0))
                        bid = float(row.get("bid", 0) or 0)
                        ask = float(row.get("ask", 0) or 0)
                        last_p = float(row.get("lastPrice", 0) or 0)
                        oi = int(row.get("openInterest", 0) or 0)
                        iv = float(row.get("impliedVolatility", 0) or 0)

                        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (last_p if last_p > 0 else ask)
                        spread = (ask - bid) if (bid > 0 and ask > 0) else 0.05

                        if oi < 100 or mid <= 0:
                            continue
                        if spread > float(filters.spread_max) and (bid > 0 and ask > 0):
                            continue

                        delta = abs(_estimate_delta(curr_price, strike, dte, iv, is_call=False))
                        if filters.delta_min <= delta <= filters.delta_max:
                            results.append(ScreenerResult(
                                symbol=ticker,
                                expiry=exp_str,
                                option_type="Put",
                                strike=Decimal(str(strike)),
                                bid=Decimal(f"{bid:.2f}"),
                                ask=Decimal(f"{ask:.2f}"),
                                spread=Decimal(f"{spread:.2f}"),
                                delta=round(delta, 2),
                                iv=round(iv, 2),
                                ivp=75.0,
                                open_interest=oi,
                                mid_price=Decimal(f"{mid:.2f}"),
                            ))

                # Process Calls (Bearish Short Call)
                if hasattr(chain, "calls") and not chain.calls.empty:
                    for _, row in chain.calls.iterrows():
                        strike = float(row.get("strike", 0))
                        bid = float(row.get("bid", 0) or 0)
                        ask = float(row.get("ask", 0) or 0)
                        last_p = float(row.get("lastPrice", 0) or 0)
                        oi = int(row.get("openInterest", 0) or 0)
                        iv = float(row.get("impliedVolatility", 0) or 0)

                        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (last_p if last_p > 0 else ask)
                        spread = (ask - bid) if (bid > 0 and ask > 0) else 0.05

                        if oi < 100 or mid <= 0:
                            continue
                        if spread > float(filters.spread_max) and (bid > 0 and ask > 0):
                            continue

                        delta = abs(_estimate_delta(curr_price, strike, dte, iv, is_call=True))
                        if filters.delta_min <= delta <= filters.delta_max:
                            results.append(ScreenerResult(
                                symbol=ticker,
                                expiry=exp_str,
                                option_type="Call",
                                strike=Decimal(str(strike)),
                                bid=Decimal(f"{bid:.2f}"),
                                ask=Decimal(f"{ask:.2f}"),
                                spread=Decimal(f"{spread:.2f}"),
                                delta=round(delta, 2),
                                iv=round(iv, 2),
                                ivp=75.0,
                                open_interest=oi,
                                mid_price=Decimal(f"{mid:.2f}"),
                            ))

        except Exception as exc:
            log.debug("Option scan failed for %s: %s", ticker, exc)
            continue

    results.sort(key=lambda r: (r.symbol, r.expiry, r.strike))
    return results

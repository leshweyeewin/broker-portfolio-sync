"""IV-Crush earnings screener — the codified moomoo Options-Playbook framework.

Automates the feasible half of the playbook's 4-step earnings filter across a
watchlist. For each name with earnings inside the horizon it computes:

  * **Step 4 — Implied vs Historical move** (the headline edge): compares the
    option market's implied Expected Move to how far the stock *actually* moves
    on earnings (``analytics.earnings_move``). ``RICH`` = market pricing more
    than history → premium worth selling; ``CHEAP`` = selling cheap → skip.
  * **Directional bias** from the trend (SMA-20 vs SMA-50 stack) → the credit
    strategy the playbook pairs with that bias.
  * **Expected-Move bounds** (upper/lower price) so the short leg can be placed
    *outside* the 1-SD move, per the deck's strike rule.

What it deliberately does NOT fake: Steps 1–3 (IV Percentile, crush consistency,
crush magnitude) need an IV history that free data doesn't retain — those light
up once the IV logger (tool #3) has accumulated snapshots. Each candidate says
so rather than inventing a letter grade from one step.

Read-only. Run:  python -m analytics.iv_crush NVDA CRM CRWD COST
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Optional

from analytics.earnings_move import historical_earnings_move, implied_vs_historical
from analytics.market_scan import (
    _annualized_realized_vol,
    _estimate_expected_move,
    get_upcoming_earnings,
)

log = logging.getLogger(__name__)

DEFAULT_DAYS_AHEAD = 14
DEFAULT_LOOKBACK = 8

# Strategy the playbook pairs with each directional bias (Step-3 "The How").
_STRATEGY_BY_BIAS = {
    "Bullish": "Put Credit Spread (sell put below lower EM)",
    "Bearish": "Call Credit Spread (sell call above upper EM)",
    "Neutral": "Iron Condor (sell both sides outside EM)",
}

# Step-4 edge → screener signal.
_SIGNAL_BY_EDGE = {
    "RICH": "SELL — rich premium",
    "FAIR": "FAIR — fair value",
    "CHEAP": "SKIP — selling cheap",
}


@dataclass
class IVCrushCandidate:
    ticker: str
    earnings_date: date
    days_left: int
    price: Optional[float] = None
    implied_move_pct: Optional[float] = None
    em_lower: Optional[float] = None
    em_upper: Optional[float] = None
    hist_move_pct: Optional[float] = None      # avg abs earnings gap (Step-4 history)
    hist_bias: str = ""                        # e.g. "6↓/2↑"
    edge: Optional[str] = None                 # RICH / FAIR / CHEAP
    bias: str = "Neutral"                      # trend bias
    strategy: str = ""

    @property
    def signal(self) -> str:
        return _SIGNAL_BY_EDGE.get(self.edge or "", "NO DATA — need history")

    def line(self) -> str:
        when = "Today!" if self.days_left == 0 else (
            "Tomorrow!" if self.days_left == 1 else f"in {self.days_left}d")
        bounds = (f" · EM ${self.em_lower:.2f}–${self.em_upper:.2f}"
                  if self.em_lower is not None else "")
        imp = f"±{self.implied_move_pct:.1f}%" if self.implied_move_pct is not None else "n/a"
        hist = f"±{self.hist_move_pct:.1f}%" if self.hist_move_pct is not None else "n/a"
        return (f"{self.ticker} — {self.earnings_date} ({when}) · {self.signal}\n"
                f"   implied {imp} vs hist {hist} {self.hist_bias} · "
                f"{self.bias} → {self.strategy}{bounds}")


def _sma(vals: list[float], window: int) -> Optional[float]:
    return statistics.fmean(vals[-window:]) if len(vals) >= window else None


def trend_bias(closes: list[float]) -> str:
    """Directional bias from the SMA-20 / SMA-50 stack (needs >=50 closes).

    ``Bullish`` when price > SMA20 > SMA50, ``Bearish`` when price < SMA20 <
    SMA50, else ``Neutral``. Neutral is also the safe default without enough data.
    """
    price = closes[-1] if closes else None
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    if price is None or sma20 is None or sma50 is None:
        return "Neutral"
    if price > sma20 > sma50:
        return "Bullish"
    if price < sma20 < sma50:
        return "Bearish"
    return "Neutral"


def em_bounds(price: float, implied_move_pct: float) -> tuple[float, float]:
    """Lower/upper 1-SD Expected-Move price bounds for strike placement."""
    delta = price * implied_move_pct / 100.0
    return round(price - delta, 2), round(price + delta, 2)


def _fetch_snapshot(tk, earnings_date: date) -> tuple[Optional[float], Optional[float], list[float]]:
    """Return (price, implied_move_pct, recent_closes) for one yfinance Ticker."""
    closes: list[float] = []
    price: Optional[float] = None
    try:
        hist = tk.history(period="4mo")
        if hist is not None and not hist.empty:
            closes = [float(c) for c in hist["Close"].dropna().tolist()]
            if closes:
                price = closes[-1]
    except Exception as exc:
        log.debug("History fetch failed: %s", exc)
    fast_info = getattr(tk, "fast_info", None)
    price = getattr(fast_info, "last_price", None) or price
    if not price:
        return None, None, closes
    implied = _estimate_expected_move(tk, earnings_date, float(price))
    return float(price), implied, closes


def scan_iv_crush(
    tickers: list[str],
    *,
    today: Optional[date] = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    lookback: int = DEFAULT_LOOKBACK,
) -> list[IVCrushCandidate]:
    """Screen a watchlist for earnings IV-crush credit-spread setups (best-effort)."""
    today = today or date.today()
    events = get_upcoming_earnings(tickers, today=today, days_ahead=days_ahead)
    if not events:
        return []

    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not installed — cannot screen IV crush")
        return []

    out: list[IVCrushCandidate] = []
    for ev in events:
        cand = IVCrushCandidate(
            ticker=ev.ticker, earnings_date=ev.earnings_date, days_left=ev.days_left)
        try:
            tk = yf.Ticker(ev.ticker)
            price, implied, closes = _fetch_snapshot(tk, ev.earnings_date)
            cand.price = price
            cand.implied_move_pct = implied
            if price and implied:
                cand.em_lower, cand.em_upper = em_bounds(price, implied)
            cand.bias = trend_bias(closes)
            cand.strategy = _STRATEGY_BY_BIAS[cand.bias]

            study = historical_earnings_move(ev.ticker, lookback=lookback, today=today)
            if study:
                cand.hist_move_pct = study.avg_abs_reaction
                cand.hist_bias = f"{study.bear_count}↓/{study.bull_count}↑"
                cand.edge = implied_vs_historical(implied, study.avg_abs_reaction)
        except Exception as exc:
            log.debug("IV-crush scan failed for %s: %s", ev.ticker, exc)
        out.append(cand)

    # RICH first, then soonest earnings.
    edge_rank = {"RICH": 0, "FAIR": 1, "CHEAP": 2, None: 3}
    out.sort(key=lambda c: (edge_rank.get(c.edge, 3), c.days_left, c.ticker))
    return out


def format_message(candidates: list[IVCrushCandidate]) -> str:
    header = (f"📅 IV-Crush Earnings Screen — {len(candidates)} upcoming "
              f"(Step 4 + bias; IV-rank steps pending logger):")
    return "\n".join([header, ""] + [c.line() for c in candidates])


def main(argv=None) -> int:
    """CLI: ``python -m analytics.iv_crush NVDA CRM [...] [--days-ahead N]``."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="IV-Crush earnings screener.")
    ap.add_argument("tickers", nargs="+", help="Watchlist ticker symbols.")
    ap.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD,
                    help="Earnings horizon in days (default %(default)s).")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help="Past earnings events for the history study (default %(default)s).")
    args = ap.parse_args(argv)

    cands = scan_iv_crush(args.tickers, days_ahead=args.days_ahead, lookback=args.lookback)
    if not cands:
        print("No upcoming earnings in horizon for the given tickers.")
        return 0
    print(format_message(cands))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

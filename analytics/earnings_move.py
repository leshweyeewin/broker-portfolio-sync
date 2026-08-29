"""Historical earnings-move study — how a stock actually moves around earnings.

The moomoo *IV-Crush* earnings playbook grades a setup on four steps; the one
step that free price data can answer directly is **Step 4 — Implied vs Historical
Move**: is the option market pricing a *bigger* move than the stock has
historically delivered on earnings (rich premium, good to sell) or a *smaller*
one (selling cheap, skip)?

This module measures the historical side of that comparison. For each of a
ticker's past earnings dates it records three moves, matching the columns in the
Options-Playbook earnings watchlist:

  * **reaction** — the earnings *gap*: the single largest daily % move in the
    ``{event day, event day + 1}`` window (covers both before-open and
    after-close reports without needing the announcement time).
  * **pre**  — drift *into* earnings over the prior ``pre_window`` trading days.
  * **post** — drift *after* the reaction over the next ``post_window`` days.

Design mirrors ``analytics.market_scan``: the measurement core is pure and
offline-testable (feed it a price series + earnings dates); yfinance is only
touched in the best-effort fetch wrapper.

Run:  python -m analytics.earnings_move NVDA CRM CRWD
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from analytics.earnings import get_earnings_dates

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK = 8      # how many past earnings events to study
DEFAULT_PRE_WINDOW = 5    # trading days of drift measured into earnings
DEFAULT_POST_WINDOW = 5   # trading days of drift measured after the reaction

# Relative band (fraction) around the historical move within which implied and
# historical are treated as roughly equal for the Step-4 edge test.
_FAIR_BAND = 0.10


@dataclass
class EarningsMove:
    """One historical earnings event's measured moves (signed %, e.g. -4.2)."""
    earnings_date: date
    reaction_day: date
    reaction_pct: float
    pre_pct: Optional[float] = None
    post_pct: Optional[float] = None


@dataclass
class EarningsMoveStudy:
    """Aggregate earnings-move statistics for one ticker."""
    ticker: str
    events: list[EarningsMove]

    @property
    def n(self) -> int:
        return len(self.events)

    @staticmethod
    def _avg_abs(vals: Sequence[Optional[float]]) -> Optional[float]:
        clean = [abs(v) for v in vals if v is not None]
        return round(sum(clean) / len(clean), 2) if clean else None

    @property
    def avg_abs_reaction(self) -> Optional[float]:
        """Average absolute earnings-gap %, the historical 'expected move'."""
        return self._avg_abs([e.reaction_pct for e in self.events])

    @property
    def avg_abs_pre(self) -> Optional[float]:
        return self._avg_abs([e.pre_pct for e in self.events])

    @property
    def avg_abs_post(self) -> Optional[float]:
        return self._avg_abs([e.post_pct for e in self.events])

    @property
    def bull_count(self) -> int:
        return sum(1 for e in self.events if e.reaction_pct > 0)

    @property
    def bear_count(self) -> int:
        return sum(1 for e in self.events if e.reaction_pct < 0)

    @property
    def directional_consistency(self) -> Optional[float]:
        """Fraction of reactions in the dominant direction (0.5 = coin-flip)."""
        if not self.events:
            return None
        return round(max(self.bull_count, self.bear_count) / self.n, 2)


def _daily_return(closes: list[float], j: int) -> Optional[float]:
    """Signed % return of ``closes[j]`` vs ``closes[j-1]`` (None if out of range)."""
    if j <= 0 or j >= len(closes):
        return None
    prev = closes[j - 1]
    if prev <= 0:
        return None
    return (closes[j] / prev - 1) * 100


def measure_earnings_moves(
    closes_by_date: Sequence[tuple[date, float]],
    earnings_dates: Sequence[date],
    *,
    pre_window: int = DEFAULT_PRE_WINDOW,
    post_window: int = DEFAULT_POST_WINDOW,
) -> list[EarningsMove]:
    """Measure reaction / pre / post moves for each earnings date. Pure + offline.

    ``closes_by_date`` must be ascending by date. An earnings date is skipped when
    there isn't a tradable day around it to measure a reaction. ``pre``/``post``
    are ``None`` when the series doesn't extend far enough — the event is still
    kept for its reaction.
    """
    series = sorted(closes_by_date, key=lambda t: t[0])
    dates = [d for d, _ in series]
    closes = [float(c) for _, c in series]

    out: list[EarningsMove] = []
    for ed in sorted(set(earnings_dates)):
        # First index on/after the earnings date.
        i = next((k for k, d in enumerate(dates) if d >= ed), None)
        if i is None or i == 0:
            continue

        # Reaction = larger-magnitude daily move of {event day, event day + 1}.
        best_j: Optional[int] = None
        best_ret: Optional[float] = None
        for j in (i, i + 1):
            r = _daily_return(closes, j)
            if r is None:
                continue
            if best_ret is None or abs(r) > abs(best_ret):
                best_j, best_ret = j, r
        if best_j is None or best_ret is None:
            continue

        # Pre-earnings drift: close[i-1] vs close[i-1-pre_window].
        pre_pct: Optional[float] = None
        p0, p1 = i - 1 - pre_window, i - 1
        if p0 >= 0 and closes[p0] > 0:
            pre_pct = round((closes[p1] / closes[p0] - 1) * 100, 2)

        # Post-reaction drift: close[reaction + post_window] vs close[reaction].
        post_pct: Optional[float] = None
        q1 = best_j + post_window
        if q1 < len(closes) and closes[best_j] > 0:
            post_pct = round((closes[q1] / closes[best_j] - 1) * 100, 2)

        out.append(EarningsMove(
            earnings_date=ed,
            reaction_day=dates[best_j],
            reaction_pct=round(best_ret, 2),
            pre_pct=pre_pct,
            post_pct=post_pct,
        ))
    return out


def implied_vs_historical(
    implied_move_pct: Optional[float],
    avg_abs_reaction_pct: Optional[float],
    *,
    band: float = _FAIR_BAND,
) -> Optional[str]:
    """Step-4 edge verdict comparing implied EM to the historical earnings gap.

    ``"RICH"``  — implied move meaningfully exceeds history (market overpricing;
    good premium to sell). ``"CHEAP"`` — implied is below history (selling cheap;
    skip). ``"FAIR"`` — within ``band`` of each other. ``None`` if either input
    is missing.
    """
    if not implied_move_pct or not avg_abs_reaction_pct or avg_abs_reaction_pct <= 0:
        return None
    ratio = implied_move_pct / avg_abs_reaction_pct
    if ratio > 1 + band:
        return "RICH"
    if ratio < 1 - band:
        return "CHEAP"
    return "FAIR"


def historical_earnings_move(
    ticker: str,
    *,
    lookback: int = DEFAULT_LOOKBACK,
    pre_window: int = DEFAULT_PRE_WINDOW,
    post_window: int = DEFAULT_POST_WINDOW,
    today: Optional[date] = None,
) -> Optional[EarningsMoveStudy]:
    """Study a ticker's last ``lookback`` *past* earnings moves (best-effort).

    Returns ``None`` when earnings dates or price history can't be obtained.
    Network-touching; the pure work is in :func:`measure_earnings_moves`.
    """
    today = today or date.today()
    ticker = ticker.upper().strip()

    past = [d for d in get_earnings_dates(ticker) if d < today]
    if not past:
        log.debug("No past earnings dates for %s", ticker)
        return None
    past = sorted(past)[-lookback:]

    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not installed — cannot study earnings moves")
        return None

    start = past[0] - timedelta(days=pre_window * 2 + 15)
    end = today + timedelta(days=1)
    try:
        hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat())
    except Exception as exc:
        log.debug("Price history fetch failed for %s: %s", ticker, exc)
        return None
    if hist is None or hist.empty:
        return None

    closes_by_date: list[tuple[date, float]] = []
    for ts, close in hist["Close"].dropna().items():
        d = ts.date() if hasattr(ts, "date") else ts
        try:
            closes_by_date.append((d, float(close)))
        except (TypeError, ValueError):
            continue

    events = measure_earnings_moves(
        closes_by_date, past, pre_window=pre_window, post_window=post_window
    )
    if not events:
        return None
    return EarningsMoveStudy(ticker=ticker, events=events)


def format_study(study: EarningsMoveStudy) -> str:
    """One-line-per-ticker summary of the earnings-move study."""
    def pct(v: Optional[float]) -> str:
        return f"{v:.1f}%" if v is not None else "n/a"

    bias = (f"{study.bull_count}↑/{study.bear_count}↓"
            f" ({study.directional_consistency:.0%} one-way)"
            if study.directional_consistency is not None else "n/a")
    return (f"{study.ticker}: gap ±{pct(study.avg_abs_reaction)} "
            f"(pre ±{pct(study.avg_abs_pre)}, post ±{pct(study.avg_abs_post)}) "
            f"over {study.n} earnings · {bias}")


def main(argv=None) -> int:
    """CLI: ``python -m analytics.earnings_move NVDA CRM [...]``."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Historical earnings-move study.")
    ap.add_argument("tickers", nargs="+", help="Ticker symbols to study.")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help="Number of past earnings events (default %(default)s).")
    args = ap.parse_args(argv)

    for t in args.tickers:
        study = historical_earnings_move(t, lookback=args.lookback)
        if study is None:
            print(f"{t.upper()}: no data")
            continue
        print(format_study(study))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

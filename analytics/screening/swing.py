"""Swing-trade technical scanner for portfolio holdings and watchlists.

Complements ``market_scan`` (which covers daily movers, earnings and short-option
income) with the *directional* technical layer a swing trader reads before an
entry: trend stack, momentum (RSI), volatility (ATR%), position vs the 20-day
mean and the 52-week high, and how close the next earnings date is.

Each ticker is reduced to one ``SwingSetup`` with a plain-English ``setup`` label
(Breakout / Pullback-buy / Uptrend / Base / Overbought / Downtrend / No-data) so
the daily brief can group names by what they're actually doing, not just by % move.

Data comes from the same batched yfinance download ``market_scan`` uses, so no new
dependency or credential. Everything fails soft: a name with no data is labelled
``No-data`` rather than sinking the scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from analytics.earnings.earnings import get_earnings_dates

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Theme grouping — the user's watchlist buckets (storage/AI/optical/…).
# A ticker not listed here falls into "Other". Kept as a flat dict (one source
# of truth) rather than per-ticker tags so the brief can group by theme.
# --------------------------------------------------------------------------- #
THEMES: dict[str, str] = {
    # Storage / Memory
    "SNDK": "Storage/Memory", "SKHY": "Storage/Memory", "DELL": "Storage/Memory",
    # AI / Compute
    "NVDA": "AI/Compute", "AVGO": "AI/Compute", "PLTR": "AI/Compute", "INTC": "AI/Compute",
    # Neo Cloud
    "NBIS": "Neo Cloud", "NET": "Neo Cloud",
    # Optical
    "LITE": "Optical",
    # MAG7 (NVDA already in AI/Compute above — first mapping wins)
    "AAPL": "MAG7", "GOOG": "MAG7", "META": "MAG7", "MSFT": "MAG7", "TSLA": "MAG7",
    # AI Power
    "CEG": "AI Power", "BE": "AI Power",
    # Crypto / treasury
    "CRCL": "Crypto/Treasury", "SBET": "Crypto/Treasury", "SPCX": "Crypto/Treasury",
    # Materials / EV / Other names on the list
    "MP": "Materials", "NIO": "China EV", "NVO": "Healthcare",
    "SHOP": "Other", "PYPL": "Other", "TEAM": "Other",
}


def theme_of(ticker: str) -> str:
    """Return the watchlist theme bucket for a ticker (``Other`` if unlisted)."""
    return THEMES.get(ticker.strip().upper(), "Other")


@dataclass
class SwingSetup:
    """One ticker's swing-relevant technical snapshot."""
    ticker: str
    price: float
    setup: str                 # Breakout | Pullback-buy | Uptrend | Base | Overbought | Downtrend | No-data
    rsi14: Optional[float] = None
    pct_vs_ma20: Optional[float] = None    # % of price above/below the 20-day SMA
    pct_vs_ma50: Optional[float] = None
    atr_pct: Optional[float] = None        # 14-day ATR as % of price (daily range / risk unit)
    pct_below_52w_high: Optional[float] = None  # how far off the 52-week high (%, >= 0)
    days_to_earnings: Optional[int] = None
    theme: str = "Other"
    note: str = ""

    @property
    def is_actionable(self) -> bool:
        """Setups a swing trader would actually look at for a long entry."""
        return self.setup in ("Breakout", "Pullback-buy")


# Setups we surface first in the brief (long-biased swing entries).
_ACTIONABLE_ORDER = {"Breakout": 0, "Pullback-buy": 1, "Uptrend": 2, "Base": 3,
                     "Overbought": 4, "Downtrend": 5, "No-data": 6}


def _clean(tickers: list[str]) -> list[str]:
    """US-equity symbols only (mirrors market_scan's filter)."""
    out: list[str] = []
    for t in tickers:
        s = str(t).strip().upper()
        if not s or "." in s or not s.isalpha() or len(s) > 5:
            continue
        if s not in out:
            out.append(s)
    return out


def _rsi(closes, period: int = 14) -> Optional[float]:
    """Wilder's RSI on a pandas close series; None if not enough data."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    ag = float(avg_gain.iloc[-1])
    al = float(avg_loss.iloc[-1])
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - (100 / (1 + rs)), 1)


def _atr_pct(hist, period: int = 14) -> Optional[float]:
    """14-day ATR as a % of last close (True Range averaged)."""
    if len(hist) < period + 1:
        return None
    high, low, close = hist["High"], hist["Low"], hist["Close"]
    prev_close = close.shift(1)
    tr = (high - low).combine((high - prev_close).abs(), max).combine(
        (low - prev_close).abs(), max
    )
    atr = float(tr.rolling(period).mean().iloc[-1])
    last = float(close.iloc[-1])
    if last <= 0:
        return None
    return round(atr / last * 100, 1)


def _classify(price, ma20, ma50, ma200, rsi, pct_below_high) -> tuple[str, str]:
    """Reduce the indicators to one setup label + a short note.

    Rules read the way a swing trader talks:
      * Breakout    — riding highs (within 3% of 52w high) with strong momentum.
      * Pullback-buy— uptrend intact (MA20>MA50) but price dipped to/under MA20
                      without breaking MA50: the classic swing entry zone.
      * Uptrend     — above a rising MA stack but not at an entry trigger.
      * Overbought  — extended (RSI>75); wait for a pullback rather than chase.
      * Downtrend   — below MA20<MA50: not a long setup.
      * Base        — none of the above (chop / undefined trend).
    """
    if None in (ma20, ma50):
        return "Base", "insufficient trend data"

    up_stack = ma20 > ma50 and (ma200 is None or ma50 > ma200)
    below_stack = ma20 < ma50

    if rsi is not None and rsi > 75:
        return "Overbought", f"RSI {rsi:.0f} — extended, wait for a pullback"
    if up_stack and pct_below_high is not None and pct_below_high <= 3 and (rsi is None or rsi >= 60):
        return "Breakout", f"within {pct_below_high:.0f}% of 52w high, RSI {rsi:.0f}" if rsi else "near 52w high"
    if up_stack and price <= ma20 and price >= ma50:
        return "Pullback-buy", "dipped to 20-DMA with 50-DMA support below"
    if up_stack and price > ma20:
        return "Uptrend", "above a rising MA stack"
    if below_stack and price < ma20:
        return "Downtrend", "below 20<50 DMA — avoid longs"
    # Fallback: distinguish a pullback that still holds the 50-DMA (long-term
    # uptrend intact, short MA just crossed under) from genuine trendless chop.
    if price > ma50:
        return "Base", "consolidating above 50-DMA — watch for 20-DMA reclaim"
    return "Base", "no defined trend"


def scan_swing_setups(
    tickers: list[str],
    *,
    today: Optional[date] = None,
    lookback: str = "1y",
) -> list[SwingSetup]:
    """Compute a swing-trade technical snapshot for each ticker.

    Batched download of daily bars, then per-ticker SMA20/50/200, RSI(14),
    ATR(14)%, 52-week-high distance and days-to-next-earnings. Sorted so the
    actionable long setups (Breakout, Pullback-buy) come first.
    """
    today = today or date.today()
    clean = _clean(tickers)
    if not clean:
        return []

    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not available for swing scan")
        return []

    try:
        data = yf.download(
            tickers=" ".join(clean),
            period=lookback,
            interval="1d",
            progress=False,
            group_by="ticker",
            auto_adjust=True,
        )
    except Exception as exc:
        log.warning("Swing scan batch download failed: %s", exc)
        return []

    results: list[SwingSetup] = []
    for ticker in clean:
        try:
            hist = data if len(clean) == 1 else (data[ticker] if ticker in data else None)
            if hist is None or hist.empty:
                results.append(SwingSetup(ticker, 0.0, "No-data", theme=theme_of(ticker)))
                continue

            closes = hist["Close"].dropna()
            if len(closes) < 20:
                results.append(SwingSetup(ticker, round(float(closes.iloc[-1]), 2) if len(closes) else 0.0,
                                          "No-data", theme=theme_of(ticker)))
                continue

            price = float(closes.iloc[-1])
            ma20 = float(closes.rolling(20).mean().iloc[-1])
            ma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
            ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
            rsi = _rsi(closes)
            atrp = _atr_pct(hist)

            high_52w = float(closes.max())
            pct_below_high = round((high_52w - price) / high_52w * 100, 1) if high_52w > 0 else None

            setup, note = _classify(price, ma20, ma50, ma200, rsi, pct_below_high)

            # Days to next earnings (swing traders size down / avoid new entries into a print).
            dte: Optional[int] = None
            for ed in get_earnings_dates(ticker):
                if ed >= today:
                    dte = (ed - today).days
                    break
            if dte is not None and dte <= 7 and setup in ("Breakout", "Pullback-buy", "Uptrend"):
                note = f"{note} · ⚠️ earnings in {dte}d"

            results.append(SwingSetup(
                ticker=ticker,
                price=round(price, 2),
                setup=setup,
                rsi14=rsi,
                pct_vs_ma20=round((price - ma20) / ma20 * 100, 1) if ma20 else None,
                pct_vs_ma50=round((price - ma50) / ma50 * 100, 1) if ma50 else None,
                atr_pct=atrp,
                pct_below_52w_high=pct_below_high,
                days_to_earnings=dte,
                theme=theme_of(ticker),
                note=note,
            ))
        except Exception as exc:
            log.debug("Swing scan failed for %s: %s", ticker, exc)
            results.append(SwingSetup(ticker, 0.0, "No-data", theme=theme_of(ticker)))

    results.sort(key=lambda s: (_ACTIONABLE_ORDER.get(s.setup, 9),
                                s.pct_below_52w_high if s.pct_below_52w_high is not None else 999))
    return results


def format_swing_message(setups: list[SwingSetup], *, max_rows: int = 12) -> str:
    """Format swing setups as a Telegram section, grouped by setup type."""
    actionable = [s for s in setups if s.is_actionable]
    if not actionable:
        return ""

    lines = ["📐 Swing Setups (technical entries):"]
    for s in actionable[:max_rows]:
        icon = "🚀" if s.setup == "Breakout" else "🎯"
        rsi = f"RSI {s.rsi14:.0f}" if s.rsi14 is not None else "RSI —"
        atr = f"ATR {s.atr_pct:.1f}%" if s.atr_pct is not None else ""
        lines.append(
            f"   {icon} {s.ticker} [{s.theme}] {s.setup} · ${s.price:,.2f} · "
            f"{rsi} · {atr} · {s.note}"
        )
    return "\n".join(lines)

"""Price-history providers for the backtester (read-only market data).

``fetch_bars`` tries Longbridge (the repo's ``LONGBRIDGE_*`` credentials) and
falls back to yfinance, returning ``Bar`` lists.  Results can be cached as the
same ``date,open,close`` CSV the CLI reads.  Prices are forward-adjusted so
splits/dividends don't fake signals.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from adapters.base import dec
from analytics.backtest.bars import Bar

log = logging.getLogger(__name__)


def longbridge_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    return t if "." in t else f"{t}.US"


def _longbridge_bars(ticker: str, start: date, end: date) -> list[Bar]:
    from longport.openapi import AdjustType, Config, Period, QuoteContext

    from adapters.longbridge import LongbridgeCredentials

    creds = LongbridgeCredentials.from_env()
    ctx = QuoteContext(Config.from_apikey(creds.app_key, creds.app_secret, creds.access_token))
    candles = ctx.history_candlesticks_by_date(
        longbridge_symbol(ticker), Period.Day, AdjustType.ForwardAdjust, start, end)
    return _to_bars((c.timestamp.date(), c.open, c.close) for c in candles)


def _yfinance_bars(ticker: str, start: date, end: date) -> list[Bar]:
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(), auto_adjust=True)
    return _to_bars((idx.date(), row["Open"], row["Close"]) for idx, row in hist.iterrows())


def _to_bars(rows) -> list[Bar]:
    bars = {d: Bar(d, dec(o), dec(c)) for d, o, c in rows if o and c}
    return [bars[d] for d in sorted(bars)]


PROVIDERS: dict[str, Callable[[str, date, date], list[Bar]]] = {
    "longbridge": _longbridge_bars,
    "yfinance": _yfinance_bars,
}


def fetch_bars(ticker: str, start: date, end: date,
               providers: Sequence[str] = ("longbridge", "yfinance")) -> tuple[list[Bar], str]:
    """First provider that returns bars wins; returns ``(bars, provider_name)``."""
    errors = []
    for name in providers:
        try:
            bars = PROVIDERS[name](ticker, start, end)
            if bars:
                return bars, name
            errors.append(f"{name}: no data")
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise assorted errors
            errors.append(f"{name}: {exc}")
            log.warning("price provider %s failed for %s: %s", name, ticker, exc)
    raise RuntimeError(f"no price data for {ticker} ({'; '.join(errors)})")


def save_csv(bars: Sequence[Bar], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "close"])
        for b in bars:
            w.writerow([b.day.isoformat(), b.open, b.close])

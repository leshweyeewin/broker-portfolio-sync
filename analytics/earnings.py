"""Earnings date lookup — API-first with static JSON fallback.

Used by the strategy tagger to classify trades that fall within ±N days of a
quarterly earnings release. Design: try yfinance first (no API key), fall back
to a local JSON cache so the module works offline and in tests.

The local cache is also written to on every successful API fetch, so repeated
runs don't hit the network for the same ticker+quarter.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CACHE_PATH = Path(__file__).parent / "earnings_dates.json"

# In-memory cache for the process lifetime — avoids re-reading the JSON file
# and re-fetching from the API on every call within a single run.
_mem_cache: dict[str, list[date]] = {}


def _load_static_cache() -> dict[str, list[str]]:
    """Load the on-disk JSON cache: {ticker: [iso-date-str, ...]}."""
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read earnings cache %s: %s", _CACHE_PATH, exc)
    return {}


def _save_static_cache(data: dict[str, list[str]]) -> None:
    """Persist the earnings cache to disk."""
    try:
        _CACHE_PATH.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("Failed to write earnings cache: %s", exc)


def _fetch_from_yfinance(ticker: str) -> Optional[list[date]]:
    """Try to pull historical earnings dates from yfinance. Returns None on failure."""
    if not ticker.isalpha() or len(ticker) > 5:
        return None
    try:
        import yfinance as yf  # type: ignore
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except ImportError:
        log.debug("yfinance not installed — skipping API earnings lookup")
        return None

    try:
        tk = yf.Ticker(ticker)
        # yfinance exposes earnings_dates as a DataFrame indexed by date
        ed = getattr(tk, "earnings_dates", None)
        if ed is None or ed.empty:
            return None
        dates = sorted(set(d.date() if hasattr(d, "date") else d for d in ed.index))
        return dates
    except Exception as exc:
        log.debug("yfinance earnings fetch failed for %s: %s", ticker, exc)
        return None


def get_earnings_dates(ticker: str) -> list[date]:
    """Return known earnings dates for ``ticker``, newest first.

    Tries static JSON cache first; on miss, tries yfinance API and caches.
    Results are cached in memory for fast repeat lookups.
    """
    ticker = ticker.upper().strip()

    # 1. In-memory cache
    if ticker in _mem_cache:
        return _mem_cache[ticker]

    # 2. Check static JSON cache first
    disk = _load_static_cache()
    if ticker in disk and disk[ticker]:
        dates = sorted(date.fromisoformat(d) for d in disk[ticker])
        _mem_cache[ticker] = dates
        return dates

    # 3. Try API if not in disk cache
    api_dates = _fetch_from_yfinance(ticker)
    if api_dates:
        _mem_cache[ticker] = api_dates
        disk[ticker] = [d.isoformat() for d in api_dates]
        _save_static_cache(disk)
        return api_dates

    # No data at all — safe default
    _mem_cache[ticker] = []
    return []


def is_near_earnings(
    ticker: str, trade_date: date, *, window_days: int = 1
) -> bool:
    """True if ``trade_date`` is within ±``window_days`` of any known earnings date."""
    for ed in get_earnings_dates(ticker):
        if abs((trade_date - ed).days) <= window_days:
            return True
    return False

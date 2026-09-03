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

_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "earnings_dates.json"

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

    # 2. Check static JSON cache
    disk = _load_static_cache()
    cached_dates: list[date] = []
    if ticker in disk and disk[ticker]:
        cached_dates = sorted(date.fromisoformat(d) for d in disk[ticker])

    today = date.today()
    # If no dates cached or all cached dates are in the past, query API to get newly scheduled dates
    if not cached_dates or max(cached_dates) <= today:
        api_dates = _fetch_from_yfinance(ticker)
        if api_dates:
            min_fresh = min(api_dates)
            merged = sorted({d for d in cached_dates if d < min_fresh} | set(api_dates))
            _mem_cache[ticker] = merged
            disk[ticker] = [d.isoformat() for d in merged]
            _save_static_cache(disk)
            return merged

    if cached_dates:
        _mem_cache[ticker] = cached_dates
        return cached_dates

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


def refresh_earnings_cache(tickers: Optional[list[str]] = None) -> dict[str, int]:
    """Re-fetch earnings dates from yfinance and merge them into the disk cache.

    Preserves older historical dates before yfinance's lookback window while
    authoritatively updating recent and future dates.
    """
    disk = _load_static_cache()
    targets = [t.upper().strip() for t in (tickers or sorted(disk.keys()))]

    result: dict[str, int] = {}
    for t in targets:
        existing = {date.fromisoformat(d) for d in disk.get(t, [])}
        fresh = _fetch_from_yfinance(t) or []
        if fresh:
            min_fresh = min(fresh)
            merged = sorted({d for d in existing if d < min_fresh} | set(fresh))
        else:
            merged = sorted(existing)
        disk[t] = [d.isoformat() for d in merged]
        result[t] = len(merged)
        if fresh:
            log.info("Refreshed %s: %d dates (+%d new)", t, len(merged),
                     len(set(fresh) - existing))
        else:
            log.warning("No fresh earnings data for %s — kept %d cached", t, len(merged))

    _save_static_cache(disk)
    _mem_cache.clear()
    return result


def main(argv=None) -> int:
    """CLI: ``python -m analytics.earnings.earnings --refresh [TICKER ...]``."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="Earnings-date cache maintenance.")
    p.add_argument("--refresh", action="store_true", help="Re-fetch & merge from yfinance.")
    p.add_argument("tickers", nargs="*", help="Tickers to refresh (default: all cached).")
    args = p.parse_args(argv)

    if not args.refresh:
        p.print_help()
        return 0

    counts = refresh_earnings_cache(args.tickers or None)
    print(f"Refreshed {len(counts)} tickers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

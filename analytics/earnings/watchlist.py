"""Derive an earnings/IV watchlist from live broker holdings.

The IV logger and earnings planner need a list of tickers to track. Instead of a
hardcoded list, build it from the underlyings actually held at chosen brokers
(default **MooMoo + Tiger**), so the daily IV snapshot and the earnings plan
follow the book. Read-only: only ``fetch_positions()`` is called — never any
order API.

Pure core (`underlyings_from_positions`) is offline-testable; the thin
`live_watchlist` shell does the network fetch and is fail-soft per broker.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from adapters.base import Broker, Position

log = logging.getLogger(__name__)

DEFAULT_BROKERS: tuple[Broker, ...] = (Broker.MOOMOO, Broker.TIGER)


def _is_us_equity_symbol(symbol: str) -> bool:
    """US-listed equity root only — yfinance IV lookups need a US ticker."""
    s = symbol.strip().upper()
    return bool(s) and s.isalpha() and len(s) <= 5


def underlyings_from_positions(
    positions: Iterable[Position],
    *,
    brokers: Sequence[Broker] = DEFAULT_BROKERS,
) -> list[str]:
    """Unique, sorted US-equity underlyings held at ``brokers`` (pure).

    For an option position ``symbol`` is already the underlying root, so both
    stock and option holdings collapse to the same ticker set. Zero-qty rows and
    non-US symbols are dropped.
    """
    wanted = set(brokers)
    out: set[str] = set()
    for pos in positions:
        if pos.broker not in wanted or pos.qty == 0:
            continue
        symbol = (pos.symbol or "").strip().upper()
        if _is_us_equity_symbol(symbol):
            out.add(symbol)
    return sorted(out)


def live_watchlist(*, brokers: Sequence[Broker] = DEFAULT_BROKERS) -> list[str]:
    """Fetch positions from ``brokers`` and reduce to a ticker watchlist.

    Fail-soft: a broker with missing credentials is never built (``_build_adapters``
    skips it), and one that errors on fetch is skipped here. Returns ``[]`` if none
    respond, so callers can fall back to a default list.
    """
    from run import _build_adapters  # lazy: keeps this module import-light + testable

    wanted_names = {b.value for b in brokers}
    positions: list[Position] = []
    for adapter in _build_adapters():
        if getattr(adapter, "name", None) not in wanted_names:
            continue
        try:
            positions.extend(adapter.fetch_positions())
        except Exception:
            log.warning("fetch_positions failed for %s", getattr(adapter, "name", "?"), exc_info=True)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    return underlyings_from_positions(positions, brokers=brokers)

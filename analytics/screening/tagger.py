"""Strategy tagging engine (Google Doc spec §2).

Classifies every trade into one of three execution buckets:

1. ``earnings_iv_crush`` — Strategy contains "IV Crush", or trade date is
   within ±1 day of the stock's quarterly earnings date.
2. ``day_trade`` — asset opened and closed on the same calendar date.
   Stocks match by Ticker; options match by Stock + Strike + Expiry + Type.
3. ``medium_term`` — position held ≥ 2 days, excluding earnings-tagged trades.

Anything that doesn't fit (e.g. opening balances, unmatched opens) is
``untagged``.

The tagger works on the flat row-dict lists returned by
``PortfolioWriter.read_all_stock_trades()`` / ``read_all_option_trades()``,
and is called during the sync so the tag lands in the sheet's Tag column.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from analytics.earnings.earnings import is_near_earnings

# Tag constants — these are the values written to the sheet's Tag column.
TAG_EARNINGS_IV_CRUSH = "Earnings IV Crush"
TAG_DAY_TRADE = "Day Trade"
TAG_MEDIUM_TERM = "Medium-Term"
TAG_UNTAGGED = ""


def tag_stock_trades(trades: list[dict[str, Any]]) -> dict[str, str]:
    """Return a mapping of dedup_key -> tag for stock trades.

    ``trades`` is the list from ``PortfolioWriter.read_all_stock_trades()``:
    each dict has ``trade`` (a StockTrade) and ``status``.
    """
    tags: dict[str, str] = {}

    # Group by (broker, ticker) to find open/close pairs
    by_instrument: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        st = t["trade"]
        by_instrument[(st.broker, st.ticker)].append(t)

    for _key, group in by_instrument.items():
        # Sort by date for pairing
        group.sort(key=lambda x: x["trade"].date)

        # Build date -> [buys], [sells] for day-trade detection
        buys_by_date: dict[date, list[dict]] = defaultdict(list)
        sells_by_date: dict[date, list[dict]] = defaultdict(list)

        for t in group:
            st = t["trade"]
            if st.action.value in ("Buy", "Opening Balance"):
                buys_by_date[st.date].append(t)
            elif st.action.value == "Sell":
                sells_by_date[st.date].append(t)

        # Day trades: buys AND sells on same date
        day_trade_keys: set[str] = set()
        for d in buys_by_date:
            if d in sells_by_date:
                for t in buys_by_date[d]:
                    day_trade_keys.add(t["trade"].dedup_key)
                for t in sells_by_date[d]:
                    day_trade_keys.add(t["trade"].dedup_key)

        # Now classify each trade
        for t in group:
            st = t["trade"]
            key = st.dedup_key

            # Priority 1: earnings IV crush
            if is_near_earnings(st.ticker, st.date):
                tags[key] = TAG_EARNINGS_IV_CRUSH
                continue

            # Priority 2: day trade
            if key in day_trade_keys:
                tags[key] = TAG_DAY_TRADE
                continue

            # Priority 3: medium-term (closed, held >= 2 days)
            if t["status"] == "Closed" and st.action.value == "Sell":
                # Find the earliest buy for this instrument
                earliest_buy = None
                for b in buys_by_date.values():
                    for bt in b:
                        if earliest_buy is None or bt["trade"].date < earliest_buy:
                            earliest_buy = bt["trade"].date
                if earliest_buy and (st.date - earliest_buy).days >= 2:
                    tags[key] = TAG_MEDIUM_TERM
                    continue

            tags[key] = TAG_UNTAGGED

    return tags


def tag_option_trades(trades: list[dict[str, Any]]) -> dict[str, str]:
    """Return a mapping of dedup_key -> tag for option trades.

    ``trades`` is the list from ``PortfolioWriter.read_all_option_trades()``:
    each dict has ``trade`` (an OptionTrade) and ``status``.
    """
    tags: dict[str, str] = {}

    # Group by (broker, underlying, strike, expiry, type) for open/close matching
    by_instrument: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        ot = t["trade"]
        by_instrument[(ot.broker, ot.underlying, ot.strike, ot.expiry, ot.option_type)].append(t)

    for _key, group in by_instrument.items():
        group.sort(key=lambda x: x["trade"].date)

        buys_by_date: dict[date, list[dict]] = defaultdict(list)
        sells_by_date: dict[date, list[dict]] = defaultdict(list)

        for t in group:
            ot = t["trade"]
            if ot.action.value in ("Buy", "Opening Balance"):
                buys_by_date[ot.date].append(t)
            elif ot.action.value == "Sell":
                sells_by_date[ot.date].append(t)

        # Day trades
        day_trade_keys: set[str] = set()
        for d in buys_by_date:
            if d in sells_by_date:
                for t in buys_by_date[d]:
                    day_trade_keys.add(t["trade"].dedup_key)
                for t in sells_by_date[d]:
                    day_trade_keys.add(t["trade"].dedup_key)

        for t in group:
            ot = t["trade"]
            key = ot.dedup_key

            # Priority 1: strategy field says IV Crush, or near earnings
            strategy = getattr(ot, "strategy", "") or ""
            if "iv crush" in strategy.lower():
                tags[key] = TAG_EARNINGS_IV_CRUSH
                continue
            if is_near_earnings(ot.underlying, ot.date):
                tags[key] = TAG_EARNINGS_IV_CRUSH
                continue

            # Priority 2: day trade
            if key in day_trade_keys:
                tags[key] = TAG_DAY_TRADE
                continue

            # Priority 3: medium-term
            if t["status"] == "Closed" and ot.action.value == "Buy":
                # For short premium: Sell-to-open, Buy-to-close
                # For long: Buy-to-open, Sell-to-close
                earliest_open = None
                # Check both sides for the earliest entry
                for d_buys in buys_by_date.values():
                    for bt in d_buys:
                        if earliest_open is None or bt["trade"].date < earliest_open:
                            earliest_open = bt["trade"].date
                for d_sells in sells_by_date.values():
                    for st in d_sells:
                        if earliest_open is None or st["trade"].date < earliest_open:
                            earliest_open = st["trade"].date
                if earliest_open and (ot.date - earliest_open).days >= 2:
                    tags[key] = TAG_MEDIUM_TERM
                    continue

            if t["status"] == "Closed" and ot.action.value == "Sell":
                earliest_open = None
                for d_buys in buys_by_date.values():
                    for bt in d_buys:
                        if earliest_open is None or bt["trade"].date < earliest_open:
                            earliest_open = bt["trade"].date
                for d_sells in sells_by_date.values():
                    for st in d_sells:
                        if earliest_open is None or st["trade"].date < earliest_open:
                            earliest_open = st["trade"].date
                if earliest_open and (ot.date - earliest_open).days >= 2:
                    tags[key] = TAG_MEDIUM_TERM
                    continue

            tags[key] = TAG_UNTAGGED

    return tags

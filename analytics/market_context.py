"""Compose a serialisable market-context card from existing analytics inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from adapters.base import dec
from analytics.market_scan import UpcomingEarnings
from analytics.option_chain import OptionChainSnapshot, OptionQuote, evaluate_quote_quality, quotes_for_expiry
from analytics.swing import SwingSetup

ZERO = Decimal("0")


@dataclass(frozen=True)
class ExpectedMove:
    expiry: date
    straddle_price: Decimal
    move_pct: Decimal
    lower: Decimal
    upper: Decimal


@dataclass(frozen=True)
class MarketContext:
    ticker: str
    price: Decimal | None
    technical: SwingSetup | None
    earnings: UpcomingEarnings | None
    iv_rank: Decimal | None
    expected_moves: tuple[ExpectedMove, ...]
    snapshot_id: str | None
    warnings: tuple[str, ...]


def expected_moves(snapshot: OptionChainSnapshot) -> tuple[ExpectedMove, ...]:
    """Derive ATM-straddle expected moves for each expiry with usable quotes."""
    if snapshot.underlying_price is None:
        return ()
    out: list[ExpectedMove] = []
    expiries = sorted({q.contract.expiry for q in snapshot.quotes})
    for expiry in expiries:
        quotes = quotes_for_expiry(snapshot, expiry)
        call = _closest_usable(quotes, "call", snapshot.underlying_price)
        put = _closest_usable(quotes, "put", snapshot.underlying_price)
        if call is None or put is None:
            continue
        call_price = evaluate_quote_quality(call).price
        put_price = evaluate_quote_quality(put).price
        if call_price is None or put_price is None:
            continue
        straddle = call_price + put_price
        if straddle <= ZERO:
            continue
        move_pct = straddle / snapshot.underlying_price * Decimal("100")
        out.append(ExpectedMove(
            expiry, straddle, move_pct,
            snapshot.underlying_price - straddle, snapshot.underlying_price + straddle,
        ))
    return tuple(out)


def build_market_context(
    ticker: str,
    *,
    snapshot: OptionChainSnapshot | None = None,
    technical: SwingSetup | None = None,
    earnings: UpcomingEarnings | None = None,
    iv_rank: Decimal | int | str | None = None,
) -> MarketContext:
    """Build a partial context card; unavailable sources become explicit warnings."""
    ticker = ticker.strip().upper()
    warnings: list[str] = []
    if not ticker:
        raise ValueError("ticker is required")
    if technical is not None and technical.ticker.upper() != ticker:
        raise ValueError("technical ticker does not match context ticker")
    if earnings is not None and earnings.ticker.upper() != ticker:
        raise ValueError("earnings ticker does not match context ticker")
    if snapshot is not None and snapshot.underlying != ticker:
        raise ValueError("snapshot underlying does not match context ticker")
    parsed_iv_rank = dec(iv_rank) if iv_rank is not None else None
    if parsed_iv_rank is not None and not ZERO <= parsed_iv_rank <= Decimal("100"):
        raise ValueError("iv_rank must be in [0, 100]")
    price = snapshot.underlying_price if snapshot else (dec(technical.price) if technical and technical.price > 0 else None)
    moves = expected_moves(snapshot) if snapshot else ()
    if snapshot is None:
        warnings.append("option-chain snapshot unavailable")
    elif snapshot.underlying_price is None:
        warnings.append("underlying price unavailable in option-chain snapshot")
    elif not moves:
        warnings.append("no usable ATM straddle to estimate expected move")
    elif any(not evaluate_quote_quality(quote).tradable for quote in snapshot.quotes):
        warnings.append("some option quotes do not meet default tradability checks")
    if technical is None:
        warnings.append("technical context unavailable")
    if earnings is None:
        warnings.append("upcoming earnings date unavailable")
    if parsed_iv_rank is None:
        warnings.append("IV rank unavailable; do not infer it from current IV")
    return MarketContext(ticker, price, technical, earnings, parsed_iv_rank, moves,
                         snapshot.snapshot_id if snapshot else None, tuple(warnings))


def _closest_usable(quotes: Sequence[OptionQuote], right: str, price: Decimal) -> OptionQuote | None:
    candidates = [q for q in quotes if q.contract.right == right and evaluate_quote_quality(q).price is not None]
    return min(candidates, key=lambda q: abs(q.contract.strike - price), default=None)

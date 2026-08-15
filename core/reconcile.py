"""Reconciliation and Seeding (step 7 of the build order — BUILD_SPEC.md §5, §9).

This module bridges the physical broker state (Positions) and our pipeline state.

1. **Seeding**: For accounts with existing history before the pipeline starts,
   we synthesize Opening Balance trades from their current open positions.
   These seed rows use a deterministic dedup key so they are never double-counted.
2. **Reconciliation**: After the FIFO engine computes holdings from all trades,
   we compare our computed Holdings against the broker's reported Positions.
   Mismatches (missed fills, corporate actions) are flagged as warnings for the Run Log.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Sequence

from adapters.base import (
    AssetType,
    OptionAction,
    OptionTrade,
    Position,
    StockAction,
    StockTrade,
)
from core.fifo_pl import Holding


def seed_positions(
    positions: Sequence[Position], seed_date: date
) -> tuple[list[StockTrade], list[OptionTrade]]:
    """Convert live broker positions into synthetic Opening Balance trades.

    Short options (and short stock) will have negative quantity in the Position,
    which flows natively into the synthetic trades. The StockTrade/OptionTrade
    classes automatically compute the correct signed total and a stable dedup key
    ("<broker>:opening:<ticker>").

    Returns
    -------
    (stocks, options)
        Lists of synthesized trades.
    """
    stocks: list[StockTrade] = []
    options: list[OptionTrade] = []

    for pos in positions:
        if pos.qty == 0:
            continue

        if pos.asset_type == AssetType.STOCK:
            stocks.append(
                StockTrade(
                    date=seed_date,
                    broker=pos.broker,
                    ticker=pos.symbol,
                    action=StockAction.OPENING_BALANCE,
                    qty=pos.qty,
                    price=pos.avg_cost,
                    fee=0,  # avg_cost is already fee-inclusive from the broker
                    currency=pos.currency,
                )
            )
        elif pos.asset_type == AssetType.OPTION:
            assert pos.option_type is not None
            assert pos.strike is not None
            assert pos.expiry is not None
            assert pos.multiplier is not None

            options.append(
                OptionTrade(
                    date=seed_date,
                    broker=pos.broker,
                    underlying=pos.symbol,
                    option_type=pos.option_type,
                    strike=pos.strike,
                    qty=pos.qty,
                    expiry=pos.expiry,
                    action=OptionAction.OPENING_BALANCE,
                    premium=pos.avg_cost,  # treat as per-share premium
                    fee=0,
                    currency=pos.currency,
                    multiplier=pos.multiplier,
                )
            )
        else:
            raise ValueError(f"Unknown asset type: {pos.asset_type}")

    return stocks, options


def _norm_strike(strike: Decimal) -> str:
    """Normalize a strike so 200 and 200.0 compare equal on both sides."""
    return format(Decimal(strike).normalize(), "f")


def _instrument_key(
    symbol: str,
    option_type,
    strike: Decimal | None,
    expiry: date | None,
) -> str:
    """Canonical instrument key built from raw components (not display strings).

    Both the pipeline (Holding) and broker (Position) sides feed their raw
    underlying symbol + option identity through here, so the keys always match.
    """
    if option_type is None:
        return symbol
    assert strike is not None
    assert expiry is not None
    return f"{symbol}:{option_type.value}:{_norm_strike(strike)}:{expiry.isoformat()}"


def expire_worthless_options(
    holdings: Sequence[Holding],
    positions: Sequence[Position],
    today: date,
) -> list[OptionTrade]:
    """Synthesize worthless-expiry closing trades for expired option holdings the
    broker no longer reports.

    When an option expires out-of-the-money the broker just drops it — no
    exercise/assignment fill is booked — so the FIFO engine would keep it open
    forever and reconciliation would flag it as "missing from broker". We close
    it at premium 0 (its worthless value), which realizes the full net premium as
    P/L and flattens the position. In-the-money expiries are closed by their real
    assignment/exercise fills upstream, so they're already flat and never reach
    here. An option that is missing from the broker but has **not** yet expired is
    left alone (a genuine gap to flag, not an expiry).

    The synthetic close carries a stable ``fill_id`` so re-runs upsert it rather
    than duplicate, and is dated on the expiry date (when the P/L is realized).
    """
    broker_open: set[tuple[str, str]] = set()
    for p in positions:
        if p.asset_type == AssetType.OPTION and p.qty != 0:
            key = _instrument_key(p.symbol, p.option_type, p.strike, p.expiry)
            broker_open.add((p.broker.value, key))

    closes: list[OptionTrade] = []
    for h in holdings:
        if h.option_type is None or h.expiry is None or h.qty == 0:
            continue
        if h.expiry >= today:
            continue  # not yet expired — leave any discrepancy to reconcile()
        key = _instrument_key(h.symbol, h.option_type, h.strike, h.expiry)
        if (h.broker.value, key) in broker_open:
            continue  # broker still holds it — not expired away
        closes.append(
            OptionTrade(
                date=h.expiry,
                broker=h.broker,
                underlying=h.symbol,
                option_type=h.option_type,
                strike=h.strike,
                qty=abs(h.qty),
                expiry=h.expiry,
                action=OptionAction.SELL if h.qty > 0 else OptionAction.BUY,
                premium=Decimal("0"),
                fee=Decimal("0"),
                currency=h.currency,
                multiplier=h.multiplier,
                fill_id=f"expiry:{key}",  # dedup_key prepends the broker
            )
        )
    return closes


def reconcile(
    holdings: Sequence[Holding], positions: Sequence[Position]
) -> list[str]:
    """Compare pipeline-computed holdings against broker-reported positions.

    Returns a list of warning strings for any discrepancies.
    """
    warnings: list[str] = []

    pipeline_map: dict[tuple[str, str], float] = {}
    broker_map: dict[tuple[str, str], float] = {}

    for h in holdings:
        # Use the raw underlying symbol (not the formatted display instrument) so
        # option keys line up with the broker side. Stocks: symbol == ticker.
        key = _instrument_key(h.symbol, h.option_type, h.strike, h.expiry)
        pipeline_map[(h.broker.value, key)] = float(h.qty)

    for p in positions:
        otype = None if p.asset_type == AssetType.STOCK else p.option_type
        key = _instrument_key(p.symbol, otype, p.strike, p.expiry)
        broker_map[(p.broker.value, key)] = broker_map.get((p.broker.value, key), 0.0) + float(p.qty)

    all_keys = set(pipeline_map.keys()) | set(broker_map.keys())
    
    for k in sorted(all_keys):
        b_name, i_key = k
        pipe_qty = pipeline_map.get(k, 0.0)
        brok_qty = broker_map.get(k, 0.0)

        # Allow minor floating point drift (shouldn't happen with Decimals but just in case)
        if abs(pipe_qty - brok_qty) > 1e-4:
            if pipe_qty == 0.0:
                warnings.append(f"[{b_name}] Missing from pipeline: {i_key} (broker reports {brok_qty})")
            elif brok_qty == 0.0:
                warnings.append(f"[{b_name}] Missing from broker: {i_key} (pipeline reports {pipe_qty})")
            else:
                warnings.append(f"[{b_name}] Qty mismatch for {i_key}: pipeline={pipe_qty}, broker={brok_qty}")

    return warnings

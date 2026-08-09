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


def reconcile(
    holdings: Sequence[Holding], positions: Sequence[Position]
) -> list[str]:
    """Compare pipeline-computed holdings against broker-reported positions.

    Returns a list of warning strings for any discrepancies.
    """
    warnings: list[str] = []

    # Build maps of (broker, instrument_key) -> qty
    # Instrument key matches what Holding uses in fifo_pl.py:
    # Stock: ticker
    # Option: ticker:type:strike:expiry
    pipeline_map: dict[tuple[str, str], float] = {}
    broker_map: dict[tuple[str, str], float] = {}

    for h in holdings:
        # Holding uses a formatted instrument string like 'AAPL' or 'AAPL PUT 150.0 2025-04-18'
        # But we also have raw fields on Holding. Let's construct a stable key.
        if h.option_type is None:
            key = h.instrument  # typically the ticker
        else:
            assert h.strike is not None
            assert h.expiry is not None
            key = f"{h.instrument}:{h.option_type.value}:{h.strike}:{h.expiry.isoformat()}"
        
        pipeline_map[(h.broker.value, key)] = float(h.qty)

    for p in positions:
        if p.asset_type == AssetType.STOCK:
            key = p.symbol
        else:
            assert p.option_type is not None
            assert p.strike is not None
            assert p.expiry is not None
            key = f"{p.symbol}:{p.option_type.value}:{p.strike}:{p.expiry.isoformat()}"
        
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

"""Broker adapters + the common schema they conform to.

Import the schema from here for lightweight (no-SDK) use:

    from adapters import StockTrade, Broker, dec

Broker adapters pull in their vendor SDKs, so import them explicitly:

    from adapters.tiger import TigerAdapter
"""

from adapters.base import (
    AssetType,
    Broker,
    BrokerAdapter,
    CashMovement,
    CashType,
    Direction,
    OptionAction,
    OptionTrade,
    OptionType,
    Position,
    PositionStatus,
    StockAction,
    StockTrade,
    dec,
    make_dedup_key,
    opening_dedup_key,
)

__all__ = [
    "AssetType",
    "Broker",
    "BrokerAdapter",
    "CashMovement",
    "CashType",
    "Direction",
    "OptionAction",
    "OptionTrade",
    "OptionType",
    "Position",
    "PositionStatus",
    "StockAction",
    "StockTrade",
    "dec",
    "make_dedup_key",
    "opening_dedup_key",
]

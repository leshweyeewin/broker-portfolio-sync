"""Provider-neutral option-chain values and quote-quality checks.

This module is deliberately offline: an adapter turns broker/yfinance data into
these models, while strategy code consumes only these models.  Missing data is
preserved as ``None`` and reported through :func:`evaluate_quote_quality`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Sequence

from adapters.base import dec

QuoteSource = Literal["broker_live", "delayed", "estimated"]
Settlement = Literal["physical", "cash", "unknown"]
Right = Literal["call", "put"]
ZERO = Decimal("0")


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    right: Right
    multiplier: Decimal = Decimal("100")
    settlement: Settlement = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip())
        object.__setattr__(self, "underlying", self.underlying.strip().upper())
        object.__setattr__(self, "strike", dec(self.strike))
        object.__setattr__(self, "multiplier", dec(self.multiplier))
        if not self.symbol or not self.underlying:
            raise ValueError("option symbol and underlying are required")
        if self.strike < ZERO or self.multiplier <= ZERO:
            raise ValueError("strike must be non-negative and multiplier positive")
        if self.right not in ("call", "put"):
            raise ValueError("right must be 'call' or 'put'")
        if self.settlement not in ("physical", "cash", "unknown"):
            raise ValueError("invalid settlement")


@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    as_of: datetime
    source: QuoteSource
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None

    def __post_init__(self) -> None:
        if self.source not in ("broker_live", "delayed", "estimated"):
            raise ValueError("invalid quote source")
        for name in ("bid", "ask", "last", "implied_volatility", "delta", "gamma", "theta", "vega"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, dec(value))
        for name in ("bid", "ask", "last", "implied_volatility"):
            value = getattr(self, name)
            if value is not None and value < ZERO:
                raise ValueError(f"{name} must be non-negative")
        for name in ("volume", "open_interest"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def midpoint(self) -> Decimal | None:
        """Strict midpoint: available only for a valid two-sided quote."""
        if self.bid is None or self.ask is None or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / Decimal("2")


@dataclass(frozen=True)
class QuoteQuality:
    price: Decimal | None
    spread: Decimal | None
    spread_pct: Decimal | None
    volume_open_interest_ratio: Decimal | None
    stale: bool
    tradable: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying: str
    underlying_price: Decimal | None
    as_of: datetime
    source: QuoteSource
    quotes: tuple[OptionQuote, ...]
    snapshot_id: str = field(default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", self.underlying.strip().upper())
        if self.underlying_price is not None:
            object.__setattr__(self, "underlying_price", dec(self.underlying_price))
            if self.underlying_price <= ZERO:
                raise ValueError("underlying_price must be positive when supplied")
        if not self.underlying:
            raise ValueError("underlying is required")
        if any(q.contract.underlying != self.underlying for q in self.quotes):
            raise ValueError("all quotes must belong to the snapshot underlying")
        if not self.snapshot_id:
            raw = "|".join(
                [self.underlying, str(self.underlying_price), self.as_of.isoformat(), self.source]
                + [f"{q.contract.symbol}:{q.bid}:{q.ask}:{q.last}:{q.as_of.isoformat()}" for q in self.quotes]
            )
            object.__setattr__(self, "snapshot_id", hashlib.sha256(raw.encode()).hexdigest()[:16])


def evaluate_quote_quality(
    quote: OptionQuote, *, min_open_interest: int = 0, max_spread_pct: Decimal | int | str = Decimal("0.15"),
    now: datetime | None = None, max_age_seconds: int | None = None,
) -> QuoteQuality:
    """Return an explainable quote usability result without inventing a price."""
    max_spread_pct = dec(max_spread_pct)
    warnings: list[str] = []
    mid = quote.midpoint
    spread: Decimal | None = None
    spread_pct: Decimal | None = None
    volume_open_interest_ratio: Decimal | None = None
    stale = False
    price = mid
    if mid is None:
        if quote.last is not None:
            price = quote.last
            warnings.append("no valid two-sided quote; using last trade only")
        else:
            warnings.append("no usable bid/ask or last price")
    else:
        spread = quote.ask - quote.bid  # midpoint guarantees both are present
        spread_pct = spread / mid if mid > ZERO else None
        if mid <= ZERO:
            warnings.append("zero midpoint")
        elif spread_pct is not None and spread_pct > max_spread_pct:
            warnings.append("bid-ask spread exceeds configured limit")
    if quote.open_interest is None:
        warnings.append("open interest unavailable")
    elif quote.open_interest < min_open_interest:
        warnings.append("open interest below configured minimum")
    if quote.volume is not None and quote.open_interest not in (None, 0):
        volume_open_interest_ratio = Decimal(quote.volume) / Decimal(quote.open_interest)
    if max_age_seconds is not None:
        reference = now or datetime.now(timezone.utc)
        quote_time = quote.as_of if quote.as_of.tzinfo else quote.as_of.replace(tzinfo=timezone.utc)
        if (reference - quote_time).total_seconds() > max_age_seconds:
            stale = True
            warnings.append("quote is stale")
    if quote.source != "broker_live":
        warnings.append(f"quote source is {quote.source}")
    tradable = price is not None and mid is not None and not stale and not any(
        warning in warnings for warning in ("bid-ask spread exceeds configured limit", "open interest below configured minimum")
    )
    return QuoteQuality(price, spread, spread_pct, volume_open_interest_ratio, stale, tradable, tuple(warnings))


def quotes_for_expiry(snapshot: OptionChainSnapshot, expiry: date) -> tuple[OptionQuote, ...]:
    return tuple(q for q in snapshot.quotes if q.contract.expiry == expiry)


class SnapshotStore:
    """Append/retrieve snapshots locally so a trade plan can retain its evidence."""

    def __init__(self, path: Path = Path("analytics") / "option_snapshots.json") -> None:
        self.path = path

    def save(self, snapshot: OptionChainSnapshot) -> OptionChainSnapshot:
        snapshots = {item.snapshot_id: item for item in self.list()}
        snapshots[snapshot.snapshot_id] = snapshot
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps([_snapshot_to_dict(s) for s in snapshots.values()], indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
        return snapshot

    def list(self) -> tuple[OptionChainSnapshot, ...]:
        if not self.path.exists():
            return ()
        return tuple(_snapshot_from_dict(raw) for raw in json.loads(self.path.read_text(encoding="utf-8")))

    def get(self, snapshot_id: str) -> OptionChainSnapshot | None:
        return next((snapshot for snapshot in self.list() if snapshot.snapshot_id == snapshot_id), None)


def _snapshot_to_dict(snapshot: OptionChainSnapshot) -> dict:
    def encode_decimal(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None
    return {
        "underlying": snapshot.underlying, "underlying_price": encode_decimal(snapshot.underlying_price),
        "as_of": snapshot.as_of.isoformat(), "source": snapshot.source, "snapshot_id": snapshot.snapshot_id,
        "quotes": [{
            "contract": {"symbol": q.contract.symbol, "underlying": q.contract.underlying,
                         "expiry": q.contract.expiry.isoformat(), "strike": str(q.contract.strike),
                         "right": q.contract.right, "multiplier": str(q.contract.multiplier), "settlement": q.contract.settlement},
            "as_of": q.as_of.isoformat(), "source": q.source, "bid": encode_decimal(q.bid), "ask": encode_decimal(q.ask),
            "last": encode_decimal(q.last), "volume": q.volume, "open_interest": q.open_interest,
            "implied_volatility": encode_decimal(q.implied_volatility), "delta": encode_decimal(q.delta),
            "gamma": encode_decimal(q.gamma), "theta": encode_decimal(q.theta), "vega": encode_decimal(q.vega),
        } for q in snapshot.quotes],
    }


def _snapshot_from_dict(raw: dict) -> OptionChainSnapshot:
    quotes = []
    for item in raw["quotes"]:
        c = item["contract"]
        contract = OptionContract(c["symbol"], c["underlying"], date.fromisoformat(c["expiry"]), c["strike"], c["right"], c["multiplier"], c["settlement"])
        quotes.append(OptionQuote(contract, datetime.fromisoformat(item["as_of"]), item["source"], item.get("bid"), item.get("ask"), item.get("last"),
                                  item.get("volume"), item.get("open_interest"), item.get("implied_volatility"), item.get("delta"),
                                  item.get("gamma"), item.get("theta"), item.get("vega")))
    return OptionChainSnapshot(raw["underlying"], raw.get("underlying_price"), datetime.fromisoformat(raw["as_of"]), raw["source"], tuple(quotes), raw["snapshot_id"])

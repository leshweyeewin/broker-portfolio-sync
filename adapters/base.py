"""Common schema — the contract every broker adapter conforms to.

This is step 1 of the build order (see BUILD_SPEC.md §10, §12). The normalized
dataclasses defined here are the *only* thing that flows between the adapters and
the rest of the pipeline: normalization, FIFO P/L, FX, dedup, and the Sheets
writer all speak this vocabulary. Nothing downstream knows which broker a row
came from except via the ``broker`` field.

Design rules pulled straight from the spec:

* **Money is ``Decimal``, never ``float``** (§4, §9). Every constructor coerces
  numeric input through :func:`dec`, which routes via ``str`` so that a stray
  ``0.1 + 0.2`` float never leaks into a money field.
* **Every money row carries an explicit currency** (§4). Currency is a required
  field on every movement/trade/position.
* **Every row gets a stable ``dedup_key``** (§6). Prefer the broker's own
  order/fill id (``broker:fill_id``); otherwise a deterministic hash of the
  business fields. Opening-balance rows get ``broker:opening:<ticker>`` so a
  re-run can never duplicate a seed row.

Sign convention for ``total`` (§4 "Qty x Price (sign per action)"):
    Buy / Opening Balance -> negative (cash outflow / cost basis established)
    Sell                  -> positive (cash inflow / proceeds)
This keeps stocks and options consistent: whatever you pay is negative, whatever
you receive is positive.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

__all__ = [
    "dec",
    "Broker",
    "CashType",
    "StockAction",
    "OptionAction",
    "OptionType",
    "Direction",
    "PositionStatus",
    "AssetType",
    "CashMovement",
    "StockTrade",
    "OptionTrade",
    "Position",
    "BrokerAdapter",
    "make_dedup_key",
    "opening_dedup_key",
    "parse_option_code",
]


# --------------------------------------------------------------------------- #
# Decimal helper
# --------------------------------------------------------------------------- #
def dec(value: object) -> Decimal:
    """Coerce ``value`` to :class:`~decimal.Decimal` without float artifacts.

    Ints, strings and Decimals convert exactly. Floats are routed through
    ``repr`` so ``0.1`` becomes ``Decimal("0.1")`` rather than the full binary
    expansion. ``None`` is rejected loudly (§9 "fail loud, never guess") — a
    money field must always have a value.
    """
    if value is None:
        raise ValueError("money/quantity value is required, got None")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass; almost never intended
        raise TypeError(f"refusing to treat bool {value!r} as a numeric amount")
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"cannot convert {value!r} to Decimal") from exc


# --------------------------------------------------------------------------- #
# Canonical enums (§8 normalization targets)
# --------------------------------------------------------------------------- #
class Broker(str, Enum):
    """Canonical broker names. Value is what lands in the sheet's Broker column."""

    LONGBRIDGE = "Longbridge"
    TIGER = "Tiger"
    MOOMOO = "MooMoo"


class CashType(str, Enum):
    """Canonical cash-movement classification (§8).

    Only ``DEPOSIT`` / ``WITHDRAWAL`` land in the Transactions tab and count
    toward Net Capital In. Dividends and fees are classified separately so
    Net Capital In stays clean; internal transfers / FX conversions are tracked
    but never counted as external capital.
    """

    DEPOSIT = "Deposit"
    WITHDRAWAL = "Withdrawal"
    DIVIDEND = "Dividend"
    FEE = "Fee"
    INTERNAL_TRANSFER = "Internal Transfer"
    FX_CONVERSION = "FX Conversion"

    @property
    def is_external_capital(self) -> bool:
        """True only for genuine external deposits/withdrawals (§8)."""
        return self in (CashType.DEPOSIT, CashType.WITHDRAWAL)


class StockAction(str, Enum):
    BUY = "Buy"
    SELL = "Sell"
    OPENING_BALANCE = "Opening Balance"  # seed only (§5)

    @property
    def is_acquisition(self) -> bool:
        """Buy and Opening Balance both establish/add to a cost-basis lot."""
        return self in (StockAction.BUY, StockAction.OPENING_BALANCE)


class OptionAction(str, Enum):
    BUY = "Buy"
    SELL = "Sell"
    OPENING_BALANCE = "Opening Balance"  # seed only (§5)

    @property
    def is_acquisition(self) -> bool:
        return self in (OptionAction.BUY, OptionAction.OPENING_BALANCE)


class OptionType(str, Enum):
    CALL = "Call"
    PUT = "Put"


class Direction(str, Enum):
    BULLISH = "Bullish"
    BEARISH = "Bearish"
    NEUTRAL = "Neutral"


class PositionStatus(str, Enum):
    OPEN = "Open"
    CLOSED = "Closed"


class AssetType(str, Enum):
    STOCK = "Stock"
    OPTION = "Option"


# --------------------------------------------------------------------------- #
# Dedup-key helpers (§6)
# --------------------------------------------------------------------------- #
def make_dedup_key(
    broker: Broker,
    fill_id: Optional[str],
    *hash_parts: object,
) -> str:
    """Build a stable dedup key (§6).

    Prefers the broker's own fill/order id -> ``"<broker>:<fill_id>"``. When no
    id is available, falls back to a short SHA-256 hash of the business fields
    passed as ``hash_parts`` -> ``"<broker>:<hash12>"``. The same inputs always
    yield the same key, so re-running the job upserts instead of duplicating.
    """
    prefix = broker.value
    if fill_id:
        return f"{prefix}:{fill_id}"
    raw = "|".join(str(p) for p in hash_parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def opening_dedup_key(broker: Broker, ticker: str) -> str:
    """Deterministic key for a seed Opening Balance row (§5, §6).

    ``"<broker>:opening:<ticker>"`` — one seed row per broker+ticker, ever.
    """
    return f"{broker.value}:opening:{ticker}"


def _signed_total(is_acquisition: bool, magnitude: Decimal) -> Decimal:
    """Apply the total sign convention: acquisitions negative, sells positive."""
    return -magnitude if is_acquisition else magnitude


_OPTION_CODE_RE = re.compile(r"^(?P<u>[A-Z]+)(?P<d>\d{6})(?P<cp>[CP])(?P<s>\d+)$")


def parse_option_code(code: str) -> Optional[tuple]:
    """Parse an OCC-style option code body into its parts, or ``None`` for a
    plain stock ticker (so callers can branch stock vs. option).

    ``"SNDQ260821P23000"`` -> ``("SNDQ", OptionType.PUT, Decimal("23"),
    date(2026, 8, 21))``. Strike is the trailing digits / 1000 (brokers such as
    MooMoo and Longbridge omit OCC's zero-padding).
    """
    m = _OPTION_CODE_RE.match(str(code).strip())
    if not m:
        return None
    otype = OptionType.CALL if m.group("cp") == "C" else OptionType.PUT
    strike = dec(int(m.group("s"))) / Decimal("1000")
    d = m.group("d")
    expiry = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
    return m.group("u"), otype, strike, expiry


# --------------------------------------------------------------------------- #
# Normalized dataclasses — the adapter <-> pipeline contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CashMovement:
    """A single cash flow: deposit, withdrawal, dividend, fee, or transfer.

    The Transactions tab (§4) only shows external Deposits/Withdrawals; the
    other ``CashType`` values are carried so normalization/dashboard logic can
    classify them out of Net Capital In.
    """

    date: date
    broker: Broker
    type: CashType
    amount: Decimal
    currency: str
    note: str = ""
    fill_id: Optional[str] = None
    dedup_key: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", dec(self.amount))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if not self.dedup_key:
            key = make_dedup_key(
                self.broker,
                self.fill_id,
                self.date.isoformat(),
                self.type.value,
                self.amount,
                self.currency,
                self.note,
            )
            object.__setattr__(self, "dedup_key", key)


@dataclass(frozen=True)
class StockTrade:
    """One stock buy/sell execution (or a synthetic Opening Balance seed row).

    ``total`` is computed from qty/price with the sign convention above unless
    explicitly supplied. ``realized_pl`` is intentionally *not* here — it is
    computed downstream by the FIFO engine (§12 step 3), not by the adapter.
    """

    date: date
    broker: Broker
    ticker: str
    action: StockAction
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    currency: str = ""
    total: Optional[Decimal] = None
    fill_id: Optional[str] = None
    dedup_key: str = field(default="", compare=False)
    # Full execution timestamp (UTC). Enables datetime-precise incremental
    # capture; None for synthetic/opening rows. Excluded from equality/dedup.
    timestamp: Optional[datetime] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.strip())
        object.__setattr__(self, "qty", dec(self.qty))
        object.__setattr__(self, "price", dec(self.price))
        object.__setattr__(self, "fee", dec(self.fee))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.total is None:
            magnitude = self.qty * self.price
            object.__setattr__(
                self, "total", _signed_total(self.action.is_acquisition, magnitude)
            )
        else:
            object.__setattr__(self, "total", dec(self.total))
        if not self.dedup_key:
            if self.action is StockAction.OPENING_BALANCE:
                key = opening_dedup_key(self.broker, self.ticker)
            else:
                key = make_dedup_key(
                    self.broker,
                    self.fill_id,
                    self.date.isoformat(),
                    self.ticker,
                    self.action.value,
                    self.qty,
                    self.price,
                )
            object.__setattr__(self, "dedup_key", key)


@dataclass(frozen=True)
class OptionTrade:
    """One option execution (or a synthetic Opening Balance seed row).

    ``total`` = Premium x Qty x multiplier, signed (§4). ``multiplier`` defaults
    to 100 but is per-broker/per-contract (§14 open item) so adapters set it
    explicitly when the API reports it.
    """

    date: date
    broker: Broker
    underlying: str
    option_type: OptionType
    strike: Decimal
    qty: Decimal
    expiry: date
    action: OptionAction
    premium: Decimal
    fee: Decimal = Decimal("0")
    currency: str = ""
    multiplier: Decimal = Decimal("100")
    strategy: str = ""
    direction: Optional[Direction] = None
    total: Optional[Decimal] = None
    fill_id: Optional[str] = None
    dedup_key: str = field(default="", compare=False)
    timestamp: Optional[datetime] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "underlying", self.underlying.strip())
        object.__setattr__(self, "strike", dec(self.strike))
        object.__setattr__(self, "qty", dec(self.qty))
        object.__setattr__(self, "premium", dec(self.premium))
        object.__setattr__(self, "fee", dec(self.fee))
        object.__setattr__(self, "multiplier", dec(self.multiplier))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.total is None:
            magnitude = self.premium * self.qty * self.multiplier
            object.__setattr__(
                self, "total", _signed_total(self.action.is_acquisition, magnitude)
            )
        else:
            object.__setattr__(self, "total", dec(self.total))
        if not self.strategy:
            prefix = "Long" if self.action.is_acquisition else "Short"
            object.__setattr__(self, "strategy", f"{prefix} {self.option_type.value.capitalize()}")
            
        if not self.dedup_key:
            if self.action is OptionAction.OPENING_BALANCE:
                # Include the full contract so distinct strikes/expiries on the
                # same underlying each get their own seed row.
                contract = f"{self.underlying}:{self.option_type.value}:{self.strike}:{self.expiry.isoformat()}"
                key = opening_dedup_key(self.broker, contract)
            else:
                key = make_dedup_key(
                    self.broker,
                    self.fill_id,
                    self.date.isoformat(),
                    self.underlying,
                    self.option_type.value,
                    self.strike,
                    self.expiry.isoformat(),
                    self.action.value,
                    self.qty,
                    self.premium,
                )
            object.__setattr__(self, "dedup_key", key)


@dataclass(frozen=True)
class Position:
    """A live broker-reported position: qty + average cost.

    Used for first-run seeding (§5) and post-write reconciliation (§9). Covers
    both stocks and options via ``asset_type``; the option fields are ``None``
    for stocks. ``avg_cost`` is per-share (stocks) or per-share-of-underlying
    (options, i.e. premium per share, not per contract) — adapters normalize to
    this so the seed Opening Balance rows line up with executions.
    """

    broker: Broker
    asset_type: AssetType
    symbol: str
    qty: Decimal
    avg_cost: Decimal
    currency: str
    market_price: Optional[Decimal] = None
    as_of: Optional[date] = None
    # Option-only fields (None for stocks)
    option_type: Optional[OptionType] = None
    strike: Optional[Decimal] = None
    expiry: Optional[date] = None
    multiplier: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip())
        object.__setattr__(self, "qty", dec(self.qty))
        object.__setattr__(self, "avg_cost", dec(self.avg_cost))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.market_price is not None:
            object.__setattr__(self, "market_price", dec(self.market_price))
        if self.strike is not None:
            object.__setattr__(self, "strike", dec(self.strike))
        object.__setattr__(self, "multiplier", dec(self.multiplier))


# --------------------------------------------------------------------------- #
# Adapter interface (§10)
# --------------------------------------------------------------------------- #
@runtime_checkable
class BrokerAdapter(Protocol):
    """Uniform contract implemented by every broker adapter (§10).

    ``since`` narrows history to executions/movements on or after that date;
    ``None`` means "everything the API will give us" (used on first run and for
    seeding). Adapters own all broker-specific quirks and must return only the
    normalized dataclasses above.

    Per §9, adapters fail loud: on API error, unexpected empty result where data
    was expected, or a changed upstream schema they raise rather than return a
    partial/guessed result.
    """

    name: str  # one of Broker's values: "Longbridge" | "Tiger" | "MooMoo"

    def fetch_cash_movements(self, since: date | None) -> list[CashMovement]: ...

    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]: ...

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]: ...

    def fetch_positions(self) -> list[Position]:
        """Current positions (qty + avg cost) for seeding + reconciliation."""
        ...

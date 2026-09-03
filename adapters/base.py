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
    "parse_option_legs",
    "is_option_code",
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


_OPTION_SINGLE_RE = re.compile(r"^(?P<u>[A-Z]+)(?P<d>\d{6})(?P<cp>[CP])(?P<s>\d+(?:\.\d+)?)$")
# General N-leg combo shorthand: UNDERLYING YYMMDD C|P STRIKE (/[C|P]STRIKE)+
# Handles 2-leg verticals, 4-leg iron condors, etc.
_OPTION_COMBO_RE = re.compile(
    r"^(?P<u>[A-Z]+)(?P<d>\d{6})(?P<cp>[CP])(?P<s>\d+(?:\.\d+)?)"
    r"(?P<rest>(?:/(?:[CP])?\d+(?:\.\d+)?)+)$"
)


def _parse_strike(s: str) -> Decimal:
    """Parse strike string from OCC 8-digit, MooMoo 1000x, or shorthand dollar values.

    OCC encodes strikes as ``price × 1000`` in 8 zero-padded digits.  Brokers
    sometimes strip leading zeros, so the raw capture can be 5–8 digits.  The
    rules:

    * **5–8 digits** → always OCC-encoded → ÷1000
      (``190000`` → 190, ``23500`` → 23.5, ``01390000`` → 1390).
    * **4 digits ending in '000'** → OCC for $1–$9 (``1000`` → 1, ``5000`` → 5).
    * **Everything else** (1–3 digits, or 4 digits not ending in '000') →
      raw dollar value, kept as-is (``190`` → 190, ``1390`` → 1390).
    """
    s = s.strip()
    if "." in s:
        return dec(s)
    ival = int(s)
    if len(s) >= 5 or (len(s) == 4 and s.endswith("000")):
        return dec(ival) / Decimal("1000")
    return dec(ival)



def _parse_single_leg(clean: str) -> Optional[tuple[str, OptionType, Decimal, date]]:
    """Parse a single clean option code body without market prefix."""
    m_single = _OPTION_SINGLE_RE.match(clean)
    if not m_single:
        return None
    u = m_single.group("u")
    d = m_single.group("d")
    expiry = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
    otype = OptionType.CALL if m_single.group("cp") == "C" else OptionType.PUT
    strike = _parse_strike(m_single.group("s"))
    return (u, otype, strike, expiry)


def parse_option_legs(code: str) -> Optional[list[tuple[str, OptionType, Decimal, date]]]:
    """Parse an option code (single-leg or multi-leg spread/combo) into leg tuples:
    ``[(underlying, OptionType, strike, expiry), ...]``.

    Returns ``None`` for plain stock tickers (e.g. ``"AAPL"``, ``"US.AAPL"``, ``"00700"``).
    Handles:
    - Standard OCC / single codes: ``"US.AAPL240119C00190000"``, ``"SHOP260821C145000"``
    - Combo spreads: ``"SHOP260821P130/145"``, ``"US.SHOP260821P130000/145000"``, ``"SHOP260821P130000/P145000"``
    - Slashed full codes: ``"US.SHOP260821P130000/US.SHOP260821P145000"``
    """
    raw = str(code).strip()
    if not raw:
        return None

    # Handle full slash-separated legs e.g. "US.SHOP260821P130000/US.SHOP260821P145000"
    if "/" in raw:
        parts = [p.strip() for p in raw.split("/")]
        if len(parts) > 1:
            full_legs = []
            for p in parts:
                # Strip prefix for each part if present
                clean_p = p
                if clean_p.startswith(("US.", "HK.", "SG.", "CN.")):
                    clean_p = clean_p[3:]
                elif clean_p.endswith((".US", ".HK", ".SG", ".CN")):
                    clean_p = clean_p[:-3]
                single = _parse_single_leg(clean_p)
                if single is None:
                    full_legs = None
                    break
                full_legs.append(single)
            if full_legs:
                return full_legs

    # Strip market prefix "US." / "HK." / "SG." or suffix ".US"
    clean = raw
    if clean.startswith(("US.", "HK.", "SG.", "CN.")):
        clean = clean[3:]
    elif clean.endswith((".US", ".HK", ".SG", ".CN")):
        clean = clean[:-3]

    # Check combo / spread shorthand (2+ legs):
    #   "SHOP260821P130/145", "SHOP260821P130000/145000"
    #   "DELL260904P400/430/C520/550" (4-leg iron condor)
    #   "DELL260904P400/430/260911P400/430" (4-leg diagonal/calendar roll)
    if "/" in clean:
        parts = clean.split("/")
        m0 = re.match(r"^(?P<u>[A-Z]+)(?P<d>\d{6})(?P<cp>[CP])(?P<s>\d+(?:\.\d+)?)$", parts[0])
        if m0:
            u = m0.group("u")
            curr_d = m0.group("d")
            curr_cp = m0.group("cp")
            expiry = date(2000 + int(curr_d[0:2]), int(curr_d[2:4]), int(curr_d[4:6]))
            otype = OptionType.CALL if curr_cp == "C" else OptionType.PUT
            legs = [(u, otype, _parse_strike(m0.group("s")), expiry)]

            sub_re = re.compile(r"^(?:(?P<d>\d{6}))?(?:(?P<cp>[CP]))?(?P<s>\d+(?:\.\d+)?)$")
            valid = True
            for p in parts[1:]:
                m = sub_re.match(p)
                if not m:
                    valid = False
                    break
                if m.group("d"):
                    curr_d = m.group("d")
                if m.group("cp"):
                    curr_cp = m.group("cp")
                expiry = date(2000 + int(curr_d[0:2]), int(curr_d[2:4]), int(curr_d[4:6]))
                otype = OptionType.CALL if curr_cp == "C" else OptionType.PUT
                strike = _parse_strike(m.group("s"))
                legs.append((u, otype, strike, expiry))
            if valid:
                return legs

    # Check single option code: "SHOP260821C145000", "AAPL240119C00190000"
    single = _parse_single_leg(clean)
    if single:
        return [single]

    return None


def is_option_code(code: str) -> bool:
    """Return True if `code` represents an option (single leg or combo/spread)."""
    return parse_option_legs(code) is not None


def parse_option_code(code: str) -> Optional[tuple[str, OptionType, Decimal, date]]:
    """Parse an OCC-style option code body into its parts, or ``None`` for a
    plain stock ticker (so callers can branch stock vs. option).

    ``"SNDQ260821P23000"`` -> ``("SNDQ", OptionType.PUT, Decimal("23"),
    date(2026, 8, 21))``.
    """
    legs = parse_option_legs(code)
    if legs and len(legs) == 1:
        return legs[0]
    return None


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
    name: str = ""  # broker-reported security name (best-effort; "" if unavailable)
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
        object.__setattr__(self, "name", (self.name or "").strip())
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

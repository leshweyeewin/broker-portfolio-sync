"""FIFO realized-P/L engine (step 3 of the build order — BUILD_SPEC.md §12).

Pure and offline: it takes normalized executions from the adapters and produces
realized P/L per closing execution plus the remaining open lots (for the
holdings summary block and reconciliation). It does **no** FX — realized P/L is
in each instrument's native currency; the FX layer (step 4) converts afterwards
at the trade-date rate (§7). Keeping FX out keeps this engine deterministic and
trivially unit-testable (§12).

Model
-----
* **Signed FIFO.** Each execution is a signed quantity: Buy ``+``, Sell ``-``.
  An execution on the same side as the current position (or from flat) *opens*
  a lot; an opposite-side execution *closes* lots FIFO, realizing P/L, and any
  overshoot flips the position into a new lot. This one model serves both long
  stock trading and the user's short-premium option strategies (sell-to-open a
  put/call, buy-to-close later).
* **Opening Balance seed rows (§5).** Treated as opening lots. Their qty is
  *signed*: positive seeds a long lot, negative seeds a short lot — so a held
  short option can be seeded correctly. Real Buy/Sell rows always carry positive
  magnitudes; only seed rows use the sign.
* **Fees (§8).** Opening fees are capitalized into the lot and released pro-rata
  as the lot is consumed; the closing execution's fee is attributed pro-rata to
  the portion it closes. Realized P/L is therefore **net of both** opening and
  closing fees. Fees are absolute cash amounts (not per-contract).
* **Options multiplier.** Per-share price differences are scaled by the contract
  multiplier (default 100, §4/§14); stocks use 1.

Not handled here (by design): option expiration / assignment produce no
execution row, so a short that expires worthless is closed by the seeding/
reconciliation layer, not this engine. Corporate actions (splits) are caught by
reconciliation (§9).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from adapters.base import (
    Broker,
    OptionAction,
    OptionTrade,
    OptionType,
    StockAction,
    StockTrade,
    dec,
)

ZERO = Decimal("0")


# --------------------------------------------------------------------------- #
# Internal lot state
# --------------------------------------------------------------------------- #
@dataclass
class _Lot:
    """An open lot: positive-magnitude qty regardless of long/short side."""

    key: str
    qty: Decimal  # remaining magnitude
    orig_qty: Decimal  # magnitude at open (for fee-per-unit)
    price: Decimal  # raw per-unit execution price
    open_fee: Decimal  # total opening fee for orig_qty
    date: date

    @property
    def open_fee_per_unit(self) -> Decimal:
        return self.open_fee / self.orig_qty if self.orig_qty else ZERO


# --------------------------------------------------------------------------- #
# Public result objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MatchedLot:
    opening_key: str
    qty: Decimal
    opening_qty: Decimal
    is_long: bool


@dataclass(frozen=True)
class Realization:
    """A single closing execution and its associated P/L."""

    key: str  # the closing execution's dedup_key
    broker: Broker
    instrument: str  # display key (ticker or option contract string)
    date: date
    qty: Decimal  # units actually closed by this execution
    close_value: Decimal  # gross cash of the closing side = price*qty*multiplier
    cost_basis: Decimal  # gross basis of closed lots (price only), *multiplier
    open_fees: Decimal  # opening fees released for the closed qty
    close_fee: Decimal  # closing-execution fee attributed to the closed qty
    realized_pl: Decimal  # net native = directional gross - open_fees - close_fee
    currency: str
    multiplier: Decimal
    components: list[MatchedLot] = field(default_factory=list)
    # Date whose FX rate values this realized P/L. Normally the close date
    # (``date``). For a zero-proceeds close (worthless expiry / assignment: the
    # broker books no closing cash), the whole P/L is the opening premium, which
    # was transacted on the opening date — so it's set to that lot's date. ``None``
    # falls back to ``date`` for callers that construct Realizations directly.
    fx_date: Optional[date] = None


@dataclass(frozen=True)
class Holding:
    """Remaining open position for an instrument after all executions."""

    broker: Broker
    instrument: str  # display key: ticker (stock) or contract string (option)
    symbol: str  # raw underlying ticker — stable key for reconciliation
    qty: Decimal  # signed: positive long, negative short
    avg_price: Decimal  # weighted-average raw price of remaining lots (positive)
    open_fees: Decimal  # opening fees still attached to remaining lots
    currency: str
    multiplier: Decimal
    as_of: Optional[date] = None
    # Option identity (None for stocks)
    option_type: Optional[OptionType] = None
    strike: Optional[Decimal] = None
    expiry: Optional[date] = None

    @property
    def avg_cost(self) -> Decimal:
        """Fee-inclusive per-unit cost (long) / credit (short) — the summary
        block's 'Avg Cost'."""
        if self.qty == 0:
            return ZERO
        per_unit_fee = self.open_fees / abs(self.qty)
        return self.avg_price + per_unit_fee if self.qty > 0 else self.avg_price - per_unit_fee

    def unrealized_pl(self, current_price: object) -> Decimal:
        """Unrealized P/L at ``current_price``, native currency (§4 summary).

        Long:  (price - avg_price) * qty * mult - open_fees
        Short: (avg_price - price) * |qty| * mult - open_fees
        """
        cp = dec(current_price)
        if self.qty > 0:
            gross = (cp - self.avg_price) * self.qty * self.multiplier
        elif self.qty < 0:
            gross = (self.avg_price - cp) * (-self.qty) * self.multiplier
        else:
            return ZERO
        return gross - self.open_fees


@dataclass(frozen=True)
class FifoResult:
    """Everything the engine produces for a set of executions."""

    realizations: list[Realization] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    fully_closed_keys: set[str] = field(default_factory=set)

    @property
    def realized_by_key(self) -> dict[str, Realization]:
        """Map a closing execution's dedup_key -> its Realization (for writing
        the Realized P/L cell onto that Sell row)."""
        return {r.key: r for r in self.realizations}

    @property
    def total_realized(self) -> Decimal:
        return sum((r.realized_pl for r in self.realizations), ZERO)


# --------------------------------------------------------------------------- #
# Internal fill representation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Fill:
    """A simplified trade for the FIFO engine."""
    key: str
    date: date
    signed_qty: Decimal  # + buy, - sell (seed rows keep their sign)
    price: Decimal
    fee: Decimal


# --------------------------------------------------------------------------- #
# Core signed-FIFO for one instrument
# --------------------------------------------------------------------------- #
def _process_instrument(
    broker: Broker,
    instrument: str,
    symbol: str,
    currency: str,
    multiplier: Decimal,
    fills: list[_Fill],
    *,
    option_type: Optional[OptionType] = None,
    strike: Optional[Decimal] = None,
    expiry: Optional[date] = None,
) -> tuple[list[Realization], Optional[Holding], set[str]]:
    lots: deque[_Lot] = deque()
    side = 0  # +1 long, -1 short, 0 flat
    realizations: list[Realization] = []
    fully_closed_keys: set[str] = set()

    for fill in fills:
        ev_side = 1 if fill.signed_qty > 0 else -1
        magnitude = abs(fill.signed_qty)
        if magnitude == 0:
            continue

        if side != 0 and ev_side != side:
            # Closing (and possibly flipping) the existing position.
            remaining = magnitude
            closed_qty = ZERO
            gross = ZERO
            cost_basis = ZERO
            open_fees_closed = ZERO
            components = []
            open_date_first: Optional[date] = None
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(remaining, lot.qty)
                if open_date_first is None:
                    open_date_first = lot.date

                # Record the matched lot for Google Sheets formulas
                components.append(MatchedLot(
                    opening_key=lot.key,
                    qty=take,
                    opening_qty=lot.orig_qty,
                    is_long=(side > 0)
                ))

                if side > 0:  # closing long via a sell at fill.price
                    gross += (fill.price - lot.price) * take
                else:  # closing short via a buy at fill.price
                    gross += (lot.price - fill.price) * take
                cost_basis += lot.price * take
                open_fees_closed += lot.open_fee_per_unit * take
                lot.qty -= take
                remaining -= take
                closed_qty += take
                if lot.qty == 0:
                    fully_closed_keys.add(lot.key)
                    lots.popleft()

            close_fee_alloc = fill.fee * (closed_qty / magnitude)
            gross *= multiplier
            cost_basis *= multiplier
            close_value = fill.price * closed_qty * multiplier
            realized_pl = gross - open_fees_closed - close_fee_alloc
            # A zero-price close means the broker booked no closing cash (a
            # worthless expiry or an assignment, synthesized upstream): the P/L is
            # entirely the opening premium, so value it at the opening-lot date.
            # Any real close keeps the close-date rate.
            fx_date = (
                open_date_first
                if fill.price == 0 and open_date_first is not None
                else fill.date
            )
            realizations.append(
                Realization(
                    key=fill.key,
                    broker=broker,
                    instrument=instrument,
                    date=fill.date,
                    qty=closed_qty,
                    close_value=close_value,
                    cost_basis=cost_basis,
                    open_fees=open_fees_closed,
                    close_fee=close_fee_alloc,
                    realized_pl=realized_pl,
                    currency=currency,
                    multiplier=multiplier,
                    components=components,
                    fx_date=fx_date,
                )
            )
            if not lots:
                side = 0
            if remaining > 0:
                # Position flipped: the overshoot opens a new lot the other way.
                open_fee_remainder = fill.fee * (remaining / magnitude)
                lots.append(_Lot(fill.key, remaining, remaining, fill.price, open_fee_remainder, fill.date))
                side = ev_side
        else:
            # Opening a lot (same side as current position, or from flat).
            lots.append(_Lot(fill.key, magnitude, magnitude, fill.price, fill.fee, fill.date))
            side = ev_side

    holding = _build_holding(
        broker, instrument, symbol, currency, multiplier, side, lots,
        option_type=option_type, strike=strike, expiry=expiry,
    )
    return realizations, holding, fully_closed_keys


def _build_holding(
    broker, instrument, symbol, currency, multiplier, side, lots,
    *, option_type, strike, expiry,
) -> Optional[Holding]:
    if not lots:
        return None
    tot_qty = sum((lot.qty for lot in lots), ZERO)
    if tot_qty == 0:
        return None
    weighted_price = sum((lot.price * lot.qty for lot in lots), ZERO)
    avg_price = weighted_price / tot_qty
    open_fees = sum((lot.open_fee_per_unit * lot.qty for lot in lots), ZERO)
    return Holding(
        broker=broker,
        instrument=instrument,
        symbol=symbol,
        qty=tot_qty * side,
        avg_price=avg_price,
        open_fees=open_fees,
        currency=currency,
        multiplier=multiplier,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
    )


# --------------------------------------------------------------------------- #
# Grouping helpers
# --------------------------------------------------------------------------- #
def _stock_signed_qty(trade: StockTrade) -> Decimal:
    if trade.action is StockAction.BUY:
        return abs(trade.qty)
    if trade.action is StockAction.SELL:
        return -abs(trade.qty)
    # OPENING_BALANCE — qty sign is meaningful (positive long, negative short)
    return trade.qty


def _option_signed_qty(trade: OptionTrade) -> Decimal:
    if trade.action is OptionAction.BUY:
        return abs(trade.qty)
    if trade.action is OptionAction.SELL:
        return -abs(trade.qty)
    return trade.qty  # OPENING_BALANCE, signed


def _resolve_currency(currencies: set[str], instrument: str) -> str:
    real = {c for c in currencies if c}
    if len(real) > 1:
        raise ValueError(
            f"instrument {instrument!r} has mixed currencies {sorted(real)}; "
            "cannot compute FIFO P/L across currencies"
        )
    return real.pop() if real else ""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compute_stock_pl(trades: list[StockTrade]) -> FifoResult:
    """FIFO realized P/L + remaining holdings for stock executions."""
    groups: dict[tuple[Broker, str], list[tuple[int, StockTrade]]] = {}
    for idx, t in enumerate(trades):
        groups.setdefault((t.broker, t.ticker), []).append((idx, t))

    all_realizations: list[Realization] = []
    all_holdings: list[Holding] = []
    all_closed_keys: set[str] = set()

    for (broker, ticker), items in groups.items():
        items.sort(key=lambda it: (it[1].date, it[0]))
        currency = _resolve_currency({t.currency for _, t in items}, ticker)
        fills = [
            _Fill(t.dedup_key, t.date, _stock_signed_qty(t), t.price, t.fee)
            for _, t in items
        ]
        r, h, closed = _process_instrument(broker, ticker, ticker, currency, Decimal("1"), fills)
        all_realizations.extend(r)
        all_closed_keys.update(closed)
        if h is not None:
            all_holdings.append(h)
    return FifoResult(all_realizations, all_holdings, all_closed_keys)


def _option_instrument(t: OptionTrade) -> str:
    return f"{t.underlying} {t.expiry.isoformat()} {t.strike} {t.option_type.value}"


def compute_option_pl(trades: list[OptionTrade]) -> FifoResult:
    """FIFO realized P/L + remaining holdings for option executions."""
    groups: dict[
        tuple[Broker, str, OptionType, Decimal, date], list[tuple[int, OptionTrade]]
    ] = {}
    for idx, t in enumerate(trades):
        key = (t.broker, t.underlying, t.option_type, t.strike, t.expiry)
        groups.setdefault(key, []).append((idx, t))

    all_realizations: list[Realization] = []
    all_holdings: list[Holding] = []
    all_closed_keys: set[str] = set()

    for (broker, underlying, otype, strike, expiry), items in groups.items():
        items.sort(key=lambda it: (it[1].date, it[0]))
        instrument = _option_instrument(items[0][1])
        currency = _resolve_currency({t.currency for _, t in items}, instrument)
        multipliers = {t.multiplier for _, t in items}
        if len(multipliers) > 1:
            raise ValueError(
                f"option {instrument!r} has mixed multipliers {sorted(multipliers)}"
            )
        multiplier = multipliers.pop()
        fills = [
            _Fill(t.dedup_key, t.date, _option_signed_qty(t), t.premium, t.fee)
            for _, t in items
        ]
        r, h, closed = _process_instrument(
            broker, instrument, underlying, currency, multiplier, fills,
            option_type=otype, strike=strike, expiry=expiry,
        )
        all_realizations.extend(r)
        all_closed_keys.update(closed)
        if h is not None:
            all_holdings.append(h)
    return FifoResult(all_realizations, all_holdings, all_closed_keys)

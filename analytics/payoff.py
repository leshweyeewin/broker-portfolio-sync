"""Pure, Decimal-based option payoff and capital-risk calculator.

The module intentionally models *expiry* payoff only.  It accepts market data
from callers but never fetches quotes, sends alerts, or submits orders.  A
strategy containing different expiries cannot be reduced to one expiry payoff
without a pricing model, so :func:`summarize_expiry` rejects it explicitly.

All figures are in the option's quote currency.  ``net_credit`` is positive
when the opening trade receives cash and negative when it pays a debit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Sequence

from adapters.base import dec

Right = Literal["call", "put"]
Side = Literal["buy", "sell"]

ZERO = Decimal("0")


@dataclass(frozen=True)
class OptionLeg:
    """One option leg entered at ``premium`` per share of underlying."""

    right: Right
    side: Side
    strike: Decimal
    premium: Decimal
    quantity: Decimal = Decimal("1")
    multiplier: Decimal = Decimal("100")
    expiry: date | None = None

    def __post_init__(self) -> None:
        if self.right not in ("call", "put"):
            raise ValueError("right must be 'call' or 'put'")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        for field in ("strike", "premium", "quantity", "multiplier"):
            object.__setattr__(self, field, dec(getattr(self, field)))
        if self.strike < ZERO:
            raise ValueError("strike must be non-negative")
        if self.premium < ZERO:
            raise ValueError("premium must be non-negative")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")
        if self.multiplier <= ZERO:
            raise ValueError("multiplier must be positive")


@dataclass(frozen=True)
class StockLeg:
    """Stock position included in an expiry strategy, e.g. a covered call."""

    side: Side
    entry_price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        object.__setattr__(self, "entry_price", dec(self.entry_price))
        object.__setattr__(self, "quantity", dec(self.quantity))
        if self.entry_price < ZERO:
            raise ValueError("entry_price must be non-negative")
        if self.quantity <= ZERO:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True)
class PayoffSummary:
    """Risk metrics at expiry.

    ``None`` means an unbounded result, never a made-up large value.  A max
    loss of ``0`` is a no-loss-or-better payoff; it does not mean no capital is
    required.  ``assignment_notional`` is the gross notional of short options
    at their strikes and is deliberately conservative for a mixed strategy.
    """

    net_credit: Decimal
    max_profit: Decimal | None
    max_loss: Decimal | None
    breakevens: tuple[Decimal, ...]
    assignment_notional: Decimal


def payoff_at_expiry(
    underlying_price: Decimal | int | float | str,
    option_legs: Sequence[OptionLeg],
    stock_legs: Sequence[StockLeg] = (),
) -> Decimal:
    """Return total strategy P/L at expiry for a non-negative stock price."""
    price = dec(underlying_price)
    if price < ZERO:
        raise ValueError("underlying_price must be non-negative")

    total = ZERO
    for leg in option_legs:
        intrinsic = max(price - leg.strike, ZERO) if leg.right == "call" else max(leg.strike - price, ZERO)
        per_share = intrinsic - leg.premium if leg.side == "buy" else leg.premium - intrinsic
        total += per_share * leg.quantity * leg.multiplier
    for leg in stock_legs:
        per_share = price - leg.entry_price if leg.side == "buy" else leg.entry_price - price
        total += per_share * leg.quantity
    return total


def summarize_expiry(
    option_legs: Sequence[OptionLeg], stock_legs: Sequence[StockLeg] = ()
) -> PayoffSummary:
    """Calculate exact extrema and breakevens for a same-expiry strategy.

    Option expiry P/L is piecewise linear, with corners at strikes.  Evaluating
    those corners and the slope of the final interval gives exact bounded and
    unbounded extrema without requiring a grid approximation.
    """
    if not option_legs and not stock_legs:
        raise ValueError("at least one option or stock leg is required")
    expiries = {leg.expiry for leg in option_legs if leg.expiry is not None}
    if len(expiries) > 1:
        raise ValueError("expiry payoff requires option legs with one common expiry")

    strikes = sorted({leg.strike for leg in option_legs})
    points = [ZERO, *strikes]
    values = [payoff_at_expiry(point, option_legs, stock_legs) for point in points]
    terminal_slope = _slope_above_all_strikes(option_legs, stock_legs)

    max_profit = None if terminal_slope > ZERO else max(values)
    min_value = None if terminal_slope < ZERO else min(values)
    max_loss = None if min_value is None else max(-min_value, ZERO)

    return PayoffSummary(
        net_credit=_net_credit(option_legs),
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=_breakevens(option_legs, stock_legs, points, values, terminal_slope),
        assignment_notional=_assignment_notional(option_legs),
    )


def long_call(strike, premium, *, quantity=1, multiplier=100, expiry: date | None = None) -> OptionLeg:
    return OptionLeg("call", "buy", strike, premium, quantity, multiplier, expiry)


def long_put(strike, premium, *, quantity=1, multiplier=100, expiry: date | None = None) -> OptionLeg:
    return OptionLeg("put", "buy", strike, premium, quantity, multiplier, expiry)


def short_call(strike, premium, *, quantity=1, multiplier=100, expiry: date | None = None) -> OptionLeg:
    return OptionLeg("call", "sell", strike, premium, quantity, multiplier, expiry)


def short_put(strike, premium, *, quantity=1, multiplier=100, expiry: date | None = None) -> OptionLeg:
    return OptionLeg("put", "sell", strike, premium, quantity, multiplier, expiry)


def bull_call_spread(long_strike, long_premium, short_strike, short_premium, **kwargs) -> tuple[OptionLeg, OptionLeg]:
    """Construct a bull call spread; raises if its strike geometry is invalid."""
    if dec(long_strike) >= dec(short_strike):
        raise ValueError("bull call spread requires long_strike < short_strike")
    return long_call(long_strike, long_premium, **kwargs), short_call(short_strike, short_premium, **kwargs)


def bear_put_spread(long_strike, long_premium, short_strike, short_premium, **kwargs) -> tuple[OptionLeg, OptionLeg]:
    """Construct a bear put spread; the long put must have the higher strike."""
    if dec(long_strike) <= dec(short_strike):
        raise ValueError("bear put spread requires long_strike > short_strike")
    return long_put(long_strike, long_premium, **kwargs), short_put(short_strike, short_premium, **kwargs)


def put_credit_spread(short_strike, short_premium, long_strike, long_premium, **kwargs) -> tuple[OptionLeg, OptionLeg]:
    if dec(short_strike) <= dec(long_strike):
        raise ValueError("put credit spread requires short_strike > long_strike")
    return short_put(short_strike, short_premium, **kwargs), long_put(long_strike, long_premium, **kwargs)


def call_credit_spread(short_strike, short_premium, long_strike, long_premium, **kwargs) -> tuple[OptionLeg, OptionLeg]:
    if dec(short_strike) >= dec(long_strike):
        raise ValueError("call credit spread requires short_strike < long_strike")
    return short_call(short_strike, short_premium, **kwargs), long_call(long_strike, long_premium, **kwargs)


def covered_call(stock_entry, shares, call_strike, call_premium, *, multiplier=100, expiry: date | None = None) -> tuple[StockLeg, OptionLeg]:
    """Construct a covered-call payoff, requiring enough shares for coverage."""
    call = short_call(call_strike, call_premium, multiplier=multiplier, expiry=expiry)
    stock = StockLeg("buy", stock_entry, shares)
    if stock.quantity < call.quantity * call.multiplier:
        raise ValueError("covered call requires at least quantity × multiplier shares")
    return stock, call


def cash_secured_put(strike, premium, *, quantity=1, multiplier=100, expiry: date | None = None) -> OptionLeg:
    """A short put; use ``assignment_notional`` for the cash-reserve estimate."""
    return short_put(strike, premium, quantity=quantity, multiplier=multiplier, expiry=expiry)


def _net_credit(option_legs: Sequence[OptionLeg]) -> Decimal:
    return sum(
        ((leg.premium if leg.side == "sell" else -leg.premium) * leg.quantity * leg.multiplier for leg in option_legs),
        ZERO,
    )


def _assignment_notional(option_legs: Sequence[OptionLeg]) -> Decimal:
    return sum((leg.strike * leg.quantity * leg.multiplier for leg in option_legs if leg.side == "sell"), ZERO)


def _slope_above_all_strikes(option_legs: Sequence[OptionLeg], stock_legs: Sequence[StockLeg]) -> Decimal:
    slope = sum(((leg.quantity * leg.multiplier) if leg.side == "buy" else -(leg.quantity * leg.multiplier)
                 for leg in option_legs if leg.right == "call"), ZERO)
    slope += sum((leg.quantity if leg.side == "buy" else -leg.quantity for leg in stock_legs), ZERO)
    return slope


def _breakevens(
    option_legs: Sequence[OptionLeg], stock_legs: Sequence[StockLeg], points: list[Decimal], values: list[Decimal], terminal_slope: Decimal
) -> tuple[Decimal, ...]:
    roots: set[Decimal] = set()
    for point, value in zip(points, values):
        if value == ZERO:
            roots.add(point)
    for start, end, start_value, end_value in zip(points, points[1:], values, values[1:]):
        if (start_value < ZERO < end_value) or (end_value < ZERO < start_value):
            slope = (end_value - start_value) / (end - start)
            roots.add(start - start_value / slope)
    final_start, final_value = points[-1], values[-1]
    if terminal_slope and final_value:
        root = final_start - final_value / terminal_slope
        if root >= final_start:
            roots.add(root)
    return tuple(sorted(roots))

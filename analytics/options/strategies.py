"""Slice 3: pure, offline credit-spread and iron-condor builder.

Turns a supplied :class:`OptionChainSnapshot` into scored, defined-risk credit
candidates (put-credit, call-credit, iron-condor).  It is deliberately *pure*:
it never fetches a chain, hits the network, or sends a notification — a caller
(``iv_crush``, ``report``, a future provider adapter) supplies the snapshot and
consumes the result.

Design rules (from the options-playbook data & calculation rules):

* ``Decimal`` throughout; premiums come from the strict quote midpoint only.
* Nothing is a buy/sell instruction — RSI is exposed as *signal context*.
* Every discarded pair is returned as a :class:`Rejection` with a reason, so a
  report can explain why no candidate is eligible instead of showing an empty
  list with no cause.
* Scores are explainable component values, never an opaque number.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Sequence

from adapters.base import dec
from analytics.options.option_chain import (
    OptionChainSnapshot,
    OptionQuote,
    QuoteQuality,
    evaluate_quote_quality,
    quotes_for_expiry,
)
from analytics.options.payoff import (
    OptionLeg,
    PayoffSummary,
    call_credit_spread,
    put_credit_spread,
    summarize_expiry,
)

log = logging.getLogger(__name__)

ZERO = Decimal("0")
ONE = Decimal("1")

Strategy = Literal["put_credit", "call_credit", "iron_condor"]

# Score weights — the relative pull of each explainable component.  Kept as
# module constants (one source of truth) rather than per-call knobs; the raw
# component values travel on every candidate so a reader can re-derive the total.
_ROR_WEIGHT = Decimal("0.40")
_BUFFER_WEIGHT = Decimal("0.30")
_LIQUIDITY_WEIGHT = Decimal("0.30")
_EVENT_PENALTY = Decimal("0.50")  # subtracted when earnings falls inside the trade
# Open interest at which the liquidity component saturates to 1.0.
_OI_REFERENCE = Decimal("500")


@dataclass(frozen=True)
class SpreadFilters:
    """Configurable, documented gates for a defined-risk seller.

    Defaults follow the playbook: 21-45 DTE, short-leg delta 0.30-0.40, at
    least 100 open interest, and a bid-ask no wider than 15% of the midpoint.
    ``expected_move_buffer`` is the fraction of spot the short strike must clear
    *beyond* the expected-move boundary; ``block_earnings`` turns an
    earnings-inside-the-trade warning into a hard rejection.
    """

    min_open_interest: int = 100
    max_spread_pct: Decimal = Decimal("0.15")
    min_dte: int = 21
    max_dte: int = 45
    short_delta_low: Decimal = Decimal("0.30")
    short_delta_high: Decimal = Decimal("0.40")
    min_width: Decimal = Decimal("1")
    max_width: Decimal = Decimal("5")
    min_credit: Decimal = Decimal("0")          # per share, must be > this
    min_return_on_risk: Decimal = Decimal("0")  # max_profit / max_loss
    expected_move_buffer: Decimal = Decimal("0")
    block_earnings: bool = False

    def __post_init__(self) -> None:
        for name in ("max_spread_pct", "short_delta_low", "short_delta_high",
                     "min_width", "max_width", "min_credit",
                     "min_return_on_risk", "expected_move_buffer"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if self.short_delta_low > self.short_delta_high:
            raise ValueError("short_delta_low must be <= short_delta_high")
        if self.min_width <= ZERO or self.max_width < self.min_width:
            raise ValueError("require 0 < min_width <= max_width")
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must be <= max_dte")


@dataclass(frozen=True)
class ScoreComponents:
    """The raw, explainable inputs behind a candidate's composite score."""

    return_on_risk: Decimal
    liquidity: Decimal            # 0..1
    expected_move_buffer: Decimal | None  # fraction beyond EM bound; None = not evaluated
    event_risk_penalty: Decimal   # 0 clean, positive value subtracted for earnings
    iv_context: Decimal | None    # optional IV-rank signal, None when unknown


@dataclass(frozen=True)
class CreditSpreadCandidate:
    underlying: str
    strategy: Strategy
    expiry: date
    dte: int
    legs: tuple[OptionLeg, ...]
    short_strikes: tuple[Decimal, ...]
    width: Decimal
    net_credit: Decimal            # total dollars received to open
    credit_per_share: Decimal
    max_profit: Decimal | None
    max_loss: Decimal | None
    return_on_risk: Decimal | None
    breakevens: tuple[Decimal, ...]
    components: ScoreComponents
    score: Decimal
    signal_context: str
    snapshot_id: str
    warnings: tuple[str, ...] = ()

    def line(self) -> str:
        """One-line, advice-free summary for a report/Telegram section."""
        rr = f"{self.return_on_risk:.2f}" if self.return_on_risk is not None else "n/a"
        strikes = "/".join(f"{s}" for s in self.short_strikes)
        return (f"{self.underlying} {self.expiry} {self.strategy} short {strikes} "
                f"w{self.width} | credit ${self.credit_per_share}/sh | "
                f"maxLoss ${self.max_loss} | R/R {rr} | score {self.score:.2f}")


@dataclass(frozen=True)
class Rejection:
    strategy: Strategy
    detail: str
    short_strike: Decimal | None = None
    long_strike: Decimal | None = None


@dataclass(frozen=True)
class SpreadScan:
    underlying: str
    expiry: date
    candidates: tuple[CreditSpreadCandidate, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Signal context — RSI as *context*, never a recommendation.
# --------------------------------------------------------------------------- #
def rsi_signal_context(rsi: float | None, strategy: Strategy) -> str:
    """Describe how the 14-day RSI reads for this structure. Not a signal."""
    if rsi is None:
        return "no RSI context (data unavailable)"
    tag = f"RSI {rsi:.0f}"
    if strategy == "put_credit":
        if rsi < 40:
            return f"{tag}: oversold zone — context aligns with a put-credit (bullish-neutral)"
        if rsi > 70:
            return f"{tag}: overbought — put-credit runs against the near-term stretch"
        return f"{tag}: neutral for a put-credit"
    if strategy == "call_credit":
        if rsi > 70:
            return f"{tag}: overbought zone — context aligns with a call-credit (bearish-neutral)"
        if rsi < 40:
            return f"{tag}: oversold — call-credit runs against the near-term stretch"
        return f"{tag}: neutral for a call-credit"
    # iron_condor
    if 40 <= rsi <= 70:
        return f"{tag}: mid-range — context aligns with a neutral iron-condor"
    return f"{tag}: outside the neutral band for an iron-condor"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_put_credit_spreads(
    snapshot: OptionChainSnapshot,
    expiry: date,
    *,
    today: date | None = None,
    filters: SpreadFilters | None = None,
    expected_move: Decimal | int | float | str | None = None,
    earnings_date: date | None = None,
    rsi: float | None = None,
    iv_rank: Decimal | int | float | str | None = None,
) -> SpreadScan:
    """Build put-credit candidates (short a higher put, long a lower put)."""
    return _build_vertical(
        "put_credit", snapshot, expiry, today=today, filters=filters,
        expected_move=expected_move, earnings_date=earnings_date, rsi=rsi, iv_rank=iv_rank,
    )


def build_call_credit_spreads(
    snapshot: OptionChainSnapshot,
    expiry: date,
    *,
    today: date | None = None,
    filters: SpreadFilters | None = None,
    expected_move: Decimal | int | float | str | None = None,
    earnings_date: date | None = None,
    rsi: float | None = None,
    iv_rank: Decimal | int | float | str | None = None,
) -> SpreadScan:
    """Build call-credit candidates (short a lower call, long a higher call)."""
    return _build_vertical(
        "call_credit", snapshot, expiry, today=today, filters=filters,
        expected_move=expected_move, earnings_date=earnings_date, rsi=rsi, iv_rank=iv_rank,
    )


def build_iron_condors(
    snapshot: OptionChainSnapshot,
    expiry: date,
    *,
    today: date | None = None,
    filters: SpreadFilters | None = None,
    expected_move: Decimal | int | float | str | None = None,
    earnings_date: date | None = None,
    rsi: float | None = None,
    iv_rank: Decimal | int | float | str | None = None,
) -> SpreadScan:
    """Pair the best put-credit and call-credit into one defined-risk condor.

    Reuses the two vertical scans so all liquidity/delta/expected-move gating and
    rejection reporting is shared; the condor is only formed when both wings
    produced at least one eligible candidate.
    """
    put_scan = build_put_credit_spreads(
        snapshot, expiry, today=today, filters=filters, expected_move=expected_move,
        earnings_date=earnings_date, rsi=rsi, iv_rank=iv_rank)
    call_scan = build_call_credit_spreads(
        snapshot, expiry, today=today, filters=filters, expected_move=expected_move,
        earnings_date=earnings_date, rsi=rsi, iv_rank=iv_rank)

    rejections = (*put_scan.rejections, *call_scan.rejections)
    warnings = tuple(dict.fromkeys((*put_scan.warnings, *call_scan.warnings)))

    if not put_scan.candidates or not call_scan.candidates:
        rejections = (*rejections, Rejection(
            "iron_condor", "need one eligible put-credit and one call-credit wing"))
        return SpreadScan(snapshot.underlying, expiry, (), rejections, warnings)

    put = put_scan.candidates[0]
    call = call_scan.candidates[0]
    legs = (*put.legs, *call.legs)
    summary = summarize_expiry(legs)
    credit_per_share = _credit_per_share(summary, legs)
    ror = _return_on_risk(summary)

    buffer_frac = _min_optional(put.components.expected_move_buffer,
                                call.components.expected_move_buffer)
    liquidity = min(put.components.liquidity, call.components.liquidity)
    penalty = put.components.event_risk_penalty  # same earnings input for both wings
    iv_component = _iv_component(iv_rank)
    components = ScoreComponents(ror or ZERO, liquidity, buffer_frac, penalty, iv_component)
    score = _score(components)
    candidate = CreditSpreadCandidate(
        underlying=snapshot.underlying, strategy="iron_condor", expiry=expiry, dte=put.dte,
        legs=legs, short_strikes=(*put.short_strikes, *call.short_strikes),
        width=max(put.width, call.width), net_credit=summary.net_credit,
        credit_per_share=credit_per_share, max_profit=summary.max_profit,
        max_loss=summary.max_loss, return_on_risk=ror, breakevens=summary.breakevens,
        components=components, score=score,
        signal_context=rsi_signal_context(rsi, "iron_condor"), snapshot_id=snapshot.snapshot_id,
        warnings=tuple(dict.fromkeys((*put.warnings, *call.warnings))),
    )
    return SpreadScan(snapshot.underlying, expiry, (candidate,), rejections, warnings)


# --------------------------------------------------------------------------- #
# Shared vertical construction
# --------------------------------------------------------------------------- #
def _build_vertical(
    strategy: Strategy,
    snapshot: OptionChainSnapshot,
    expiry: date,
    *,
    today: date | None,
    filters: SpreadFilters | None,
    expected_move,
    earnings_date: date | None,
    rsi: float | None,
    iv_rank,
) -> SpreadScan:
    filt = filters or SpreadFilters()
    today = today or date.today()
    right = "put" if strategy == "put_credit" else "call"
    price = snapshot.underlying_price
    em = dec(expected_move) if expected_move is not None else None
    iv_component = _iv_component(iv_rank)

    scan_warnings: list[str] = []
    rejections: list[Rejection] = []

    dte = (expiry - today).days
    if dte < filt.min_dte or dte > filt.max_dte:
        rejections.append(Rejection(strategy, f"DTE {dte} outside [{filt.min_dte},{filt.max_dte}]"))
        return SpreadScan(snapshot.underlying, expiry, (), tuple(rejections), tuple(scan_warnings))

    quotes = [q for q in quotes_for_expiry(snapshot, expiry) if q.contract.right == right]
    if not quotes:
        rejections.append(Rejection(strategy, f"no {right} quotes for {expiry}"))
        return SpreadScan(snapshot.underlying, expiry, (), tuple(rejections), tuple(scan_warnings))

    if em is None or price is None:
        scan_warnings.append("expected-move buffer not evaluated (missing expected move or price)")

    # Earnings event risk applies to the whole expiry: a print between now and
    # expiry is the classic IV-crush / gap exposure a credit seller must see.
    earnings_inside = earnings_date is not None and today <= earnings_date <= expiry
    if earnings_inside and filt.block_earnings:
        rejections.append(Rejection(strategy, f"earnings {earnings_date} inside trade window (blocked)"))
        return SpreadScan(snapshot.underlying, expiry, (), tuple(rejections), tuple(scan_warnings))
    event_penalty = _EVENT_PENALTY if earnings_inside else ZERO

    by_strike = {q.contract.strike: q for q in quotes}
    # Short candidates run from the money outward (closest strike first).
    shorts = sorted(quotes, key=lambda q: q.contract.strike, reverse=(right == "put"))

    candidates: list[CreditSpreadCandidate] = []
    for short_q in shorts:
        short_strike = short_q.contract.strike
        short_quality, reason = _delta_and_quality(short_q, filt)
        if reason is not None:
            rejections.append(Rejection(strategy, reason, short_strike=short_strike))
            continue

        long_q = _pick_long(right, short_strike, by_strike, filt)
        if long_q is None:
            rejections.append(Rejection(strategy, f"no long {right} within width "
                                        f"[{filt.min_width},{filt.max_width}]", short_strike=short_strike))
            continue
        long_strike = long_q.contract.strike
        long_quality = evaluate_quote_quality(
            long_q, min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct)
        if long_quality.price is None:
            rejections.append(Rejection(strategy, "long leg has no usable price",
                                        short_strike=short_strike, long_strike=long_strike))
            continue

        try:
            if strategy == "put_credit":
                legs = put_credit_spread(short_strike, short_quality.price, long_strike, long_quality.price)
            else:
                legs = call_credit_spread(short_strike, short_quality.price, long_strike, long_quality.price)
        except ValueError as exc:  # invalid geometry, e.g. short/long crossed
            rejections.append(Rejection(strategy, f"invalid geometry: {exc}",
                                        short_strike=short_strike, long_strike=long_strike))
            continue

        summary = summarize_expiry(legs)
        credit_per_share = _credit_per_share(summary, legs)
        if credit_per_share <= filt.min_credit:
            rejections.append(Rejection(strategy, f"credit ${credit_per_share}/sh not above min "
                                        f"${filt.min_credit}", short_strike=short_strike, long_strike=long_strike))
            continue
        ror = _return_on_risk(summary)
        if ror is not None and ror < filt.min_return_on_risk:
            rejections.append(Rejection(strategy, f"return-on-risk {ror:.2f} below min "
                                        f"{filt.min_return_on_risk}", short_strike=short_strike, long_strike=long_strike))
            continue

        leg_warnings: list[str] = []
        for quality in (short_quality, long_quality):
            leg_warnings.extend(quality.warnings)

        buffer_frac = _expected_move_buffer(strategy, short_strike, price, em)
        if buffer_frac is not None and buffer_frac < filt.expected_move_buffer:
            leg_warnings.append(f"short strike inside expected-move buffer "
                                f"({buffer_frac:.3f} < {filt.expected_move_buffer})")
        if earnings_inside:
            leg_warnings.append(f"earnings {earnings_date} inside trade window (event risk)")

        liquidity = _liquidity_score(short_quality, long_quality, filt)
        components = ScoreComponents(ror or ZERO, liquidity, buffer_frac, event_penalty, iv_component)
        candidates.append(CreditSpreadCandidate(
            underlying=snapshot.underlying, strategy=strategy, expiry=expiry, dte=dte,
            legs=legs, short_strikes=(short_strike,), width=abs(short_strike - long_strike),
            net_credit=summary.net_credit, credit_per_share=credit_per_share,
            max_profit=summary.max_profit, max_loss=summary.max_loss, return_on_risk=ror,
            breakevens=summary.breakevens, components=components, score=_score(components),
            signal_context=rsi_signal_context(rsi, strategy), snapshot_id=snapshot.snapshot_id,
            warnings=tuple(dict.fromkeys(leg_warnings)),
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return SpreadScan(snapshot.underlying, expiry, tuple(candidates), tuple(rejections), tuple(scan_warnings))


def _delta_and_quality(quote: OptionQuote, filt: SpreadFilters) -> tuple[QuoteQuality, str | None]:
    """Gate a prospective short leg on liquidity then delta band.

    Returns ``(quality, None)`` when eligible, or ``(quality, reason)`` when it
    should be rejected — delta is required (fail safe, never fabricate a
    probability proxy when the chain omits Greeks).
    """
    quality = evaluate_quote_quality(
        quote, min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct)
    if quality.price is None:
        return quality, "short leg has no usable two-sided price"
    if not quality.tradable:
        return quality, f"short leg not tradable ({'; '.join(quality.warnings) or 'quality'})"
    if quote.delta is None:
        return quality, "short leg delta unavailable (cannot verify probability band)"
    mag = abs(quote.delta)
    if mag < filt.short_delta_low or mag > filt.short_delta_high:
        return quality, f"short delta {mag} outside [{filt.short_delta_low},{filt.short_delta_high}]"
    return quality, None


def _pick_long(right: str, short_strike: Decimal, by_strike: dict[Decimal, OptionQuote],
               filt: SpreadFilters) -> OptionQuote | None:
    """Nearest protective long strike within [min_width, max_width] of the short."""
    if right == "put":  # protective long sits below the short put
        strikes = sorted((s for s in by_strike if s < short_strike), reverse=True)
    else:               # protective long sits above the short call
        strikes = sorted(s for s in by_strike if s > short_strike)
    for strike in strikes:
        width = abs(short_strike - strike)
        if filt.min_width <= width <= filt.max_width:
            return by_strike[strike]
    return None


# --------------------------------------------------------------------------- #
# Metrics & scoring
# --------------------------------------------------------------------------- #
def _credit_per_share(summary: PayoffSummary, legs: Sequence[OptionLeg]) -> Decimal:
    """Net credit expressed per share of underlying (undo qty × multiplier)."""
    scale = legs[0].quantity * legs[0].multiplier
    return summary.net_credit / scale if scale else summary.net_credit


def _return_on_risk(summary: PayoffSummary) -> Decimal | None:
    if summary.max_loss is None or summary.max_loss <= ZERO or summary.max_profit is None:
        return None
    return summary.max_profit / summary.max_loss


def _expected_move_buffer(strategy: Strategy, short_strike: Decimal,
                          price: Decimal | None, em: Decimal | None) -> Decimal | None:
    """Fraction of spot the short strike clears the expected-move boundary by.

    Positive = short strike sits safely outside the 1-EM band; negative = inside.
    ``None`` when expected move or spot is unavailable (never guessed).
    """
    if price is None or em is None or price <= ZERO:
        return None
    if strategy == "put_credit":
        lower = price - em
        return (lower - short_strike) / price
    if strategy == "call_credit":
        upper = price + em
        return (short_strike - upper) / price
    return None


def _liquidity_score(short_q: QuoteQuality, long_q: QuoteQuality, filt: SpreadFilters) -> Decimal:
    """0..1 liquidity from the worse leg's open interest and bid-ask tightness."""
    def leg_score(quality: QuoteQuality) -> Decimal:
        # Open-interest component saturates at _OI_REFERENCE; unknown OI scores 0.
        # (evaluate_quote_quality already surfaced the missing-OI warning.)
        spread_pct = quality.spread_pct
        spread_component = ONE
        if spread_pct is not None and filt.max_spread_pct > ZERO:
            spread_component = max(ZERO, ONE - spread_pct / filt.max_spread_pct)
        return spread_component
    return min(leg_score(short_q), leg_score(long_q))


def _iv_component(iv_rank) -> Decimal | None:
    """Optional IV-rank signal in 0..1; ``None`` when no rank was supplied.

    IV rank must be a measured value from the caller — this module never
    fabricates one from current IV (IV rank must be measured, never proxied).
    """
    if iv_rank is None:
        return None
    value = dec(iv_rank)
    if value > ONE:  # accept either 0..1 or 0..100 conventions
        value = value / Decimal("100")
    return min(ONE, max(ZERO, value))


def _score(components: ScoreComponents) -> Decimal:
    """Weighted, explainable composite. Components travel with the candidate."""
    ror = min(components.return_on_risk, ONE)  # cap so a thin-wing R/R can't dominate
    buffer = components.expected_move_buffer if components.expected_move_buffer is not None else ZERO
    buffer = max(ZERO, min(buffer, Decimal("0.20")))  # 0..0.20 → 0..1 after weight
    buffer_scaled = buffer / Decimal("0.20")
    total = (_ROR_WEIGHT * ror + _BUFFER_WEIGHT * buffer_scaled
             + _LIQUIDITY_WEIGHT * components.liquidity - components.event_risk_penalty)
    if components.iv_context is not None:
        total += _LIQUIDITY_WEIGHT * components.iv_context * Decimal("0.5")
    return total


def _min_optional(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None



from adapters.base import dec
from analytics.options.payoff import (
    OptionLeg,
    PayoffSummary,
    bull_call_spread,
    bear_put_spread,
    long_call,
    long_put,
    summarize_expiry,
)

@dataclass(frozen=True)
class DirectionalFilters:
    min_open_interest: int = 100
    max_spread_pct: Decimal = Decimal("0.15")
    min_dte: int = 14
    max_dte: int = 60
    long_delta_low: Decimal = Decimal("0.40")
    long_delta_high: Decimal = Decimal("0.70")
    min_width: Decimal = Decimal("1")
    max_width: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        for name in ("max_spread_pct", "long_delta_low", "long_delta_high", "min_width", "max_width"):
            object.__setattr__(self, name, dec(getattr(self, name)))

@dataclass(frozen=True)
class DirectionalCandidate:
    underlying: str
    strategy: str
    expiry: date
    dte: int
    legs: tuple
    net_debit: Decimal
    debit_per_share: Decimal
    max_profit: Decimal | None
    max_loss: Decimal | None
    return_on_risk: Decimal | None
    breakevens: tuple
    signal_context: str
    snapshot_id: str
    warnings: tuple = ()

@dataclass(frozen=True)
class DirectionalScan:
    underlying: str
    expiry: date
    candidates: tuple = ()
    rejections: tuple = ()
    warnings: tuple = ()

def _build_directional(
    strategy: str,
    snapshot,
    expiry: date,
    *,
    today: date | None,
    filters: DirectionalFilters | None,
    rsi: float | None,
) -> DirectionalScan:
    filt = filters or DirectionalFilters()
    today = today or date.today()
    right = "put" if strategy in ("bear_put", "long_put") else "call"
    
    scan_warnings = []
    rejections = []
    candidates = []
    
    dte = (expiry - today).days
    if dte < filt.min_dte or dte > filt.max_dte:
        rejections.append(Rejection(strategy, f"DTE {dte} outside [{filt.min_dte},{filt.max_dte}]"))
        return DirectionalScan(snapshot.underlying, expiry, (), tuple(rejections), tuple(scan_warnings))
        
    quotes = [q for q in quotes_for_expiry(snapshot, expiry) if q.contract.right == right]
    if not quotes:
        rejections.append(Rejection(strategy, f"no {right} quotes"))
        return DirectionalScan(snapshot.underlying, expiry, (), tuple(rejections), tuple(scan_warnings))
        
    by_strike = {q.contract.strike: q for q in quotes}
    
    for long_q in quotes:
        long_strike = long_q.contract.strike
        quality = evaluate_quote_quality(long_q, min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct)
        if not quality.tradable or quality.price is None: continue
        
        delta = abs(float(long_q.delta)) if long_q.delta else 0
        if delta < float(filt.long_delta_low) or delta > float(filt.long_delta_high): continue
        
        # If long option
        if strategy in ("long_call", "long_put"):
            legs = (long_call(long_strike, quality.price) if right == "call" else long_put(long_strike, quality.price),)
            summary = summarize_expiry(legs)
            
            candidates.append(DirectionalCandidate(
                underlying=snapshot.underlying, strategy=strategy, expiry=expiry, dte=dte, legs=legs,
                net_debit=-summary.net_credit, debit_per_share=-summary.net_credit/100,
                max_profit=summary.max_profit, max_loss=summary.max_loss, return_on_risk=None,
                breakevens=summary.breakevens, signal_context=rsi_signal_context(rsi, "iron_condor"),
                snapshot_id=snapshot.snapshot_id, warnings=tuple(quality.warnings)
            ))
        else:
            # Debit spread
            short_q = _pick_short_for_debit(right, long_strike, by_strike, filt)
            if not short_q: continue
            short_strike = short_q.contract.strike
            s_quality = evaluate_quote_quality(short_q, min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct)
            if not s_quality.tradable or s_quality.price is None: continue
            
            try:
                legs = bull_call_spread(long_strike, quality.price, short_strike, s_quality.price) if right == "call" else bear_put_spread(long_strike, quality.price, short_strike, s_quality.price)
                summary = summarize_expiry(legs)
                debit = -summary.net_credit
                if debit <= 0: continue
                
                candidates.append(DirectionalCandidate(
                    underlying=snapshot.underlying, strategy=strategy, expiry=expiry, dte=dte, legs=legs,
                    net_debit=debit, debit_per_share=debit/100, max_profit=summary.max_profit, max_loss=summary.max_loss,
                    return_on_risk=summary.max_profit/summary.max_loss if summary.max_loss else None,
                    breakevens=summary.breakevens, signal_context=rsi_signal_context(rsi, "iron_condor"),
                    snapshot_id=snapshot.snapshot_id, warnings=tuple(dict.fromkeys((*quality.warnings, *s_quality.warnings)))
                ))
            except: continue
            
    return DirectionalScan(snapshot.underlying, expiry, tuple(candidates), tuple(rejections), tuple(scan_warnings))

def _pick_short_for_debit(right, long_strike, by_strike, filt):
    if right == "call":
        strikes = sorted(s for s in by_strike if s > long_strike)
    else:
        strikes = sorted((s for s in by_strike if s < long_strike), reverse=True)
    for strike in strikes:
        width = abs(long_strike - strike)
        if filt.min_width <= width <= filt.max_width:
            return by_strike[strike]
    return None

def build_bull_call_spreads(snapshot, expiry, *, today=None, filters=None, rsi=None):
    return _build_directional("bull_call", snapshot, expiry, today=today, filters=filters, rsi=rsi)

def build_bear_put_spreads(snapshot, expiry, *, today=None, filters=None, rsi=None):
    return _build_directional("bear_put", snapshot, expiry, today=today, filters=filters, rsi=rsi)

def build_long_calls(snapshot, expiry, *, today=None, filters=None, rsi=None):
    return _build_directional("long_call", snapshot, expiry, today=today, filters=filters, rsi=rsi)

def build_long_puts(snapshot, expiry, *, today=None, filters=None, rsi=None):
    return _build_directional("long_put", snapshot, expiry, today=today, filters=filters, rsi=rsi)


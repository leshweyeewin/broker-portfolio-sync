"""Directional & income strategy builders (debit spreads, long options, Wheel, PMCC).

Pure, offline builders that consume an :class:`OptionChainSnapshot` and return
scored candidates — plus a thin CLI that fetches a snapshot per ticker and prints
the top candidate for each structure.  Like ``strategies`` (the credit-spread /
iron-condor module it shares :class:`Rejection`, :class:`SpreadFilters` and
``rsi_signal_context`` with) it never places an order or gives advice.

Run: python -m analytics.options.directional_builder AAPL MSFT
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from adapters.base import dec
from analytics.screening.screener import _build_quote_client, fetch_option_chain
from analytics.options.option_chain import (
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    evaluate_quote_quality,
    quotes_for_expiry,
)
from analytics.options.payoff import (
    bear_put_spread,
    bull_call_spread,
    long_call,
    long_put,
    short_call,
    short_put,
    summarize_expiry,
)
from analytics.options.strategies import Rejection, SpreadFilters, rsi_signal_context

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Directional builders (debit spreads + single long/short legs)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DirectionalFilters:
    min_open_interest: int = 100
    max_spread_pct: Decimal = Decimal("0.15")
    min_dte: int = 30
    max_dte: int = 45
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
    right = "put" if strategy in ("bear_put", "long_put", "short_put") else "call"

    scan_warnings: list[str] = []
    rejections: list[Rejection] = []
    candidates: list[DirectionalCandidate] = []

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
        if not quality.tradable or quality.price is None:
            continue

        delta = abs(float(long_q.delta)) if long_q.delta else 0
        if delta < float(filt.long_delta_low) or delta > float(filt.long_delta_high):
            continue

        # Single-leg long/short options.
        if strategy in ("long_call", "long_put", "short_call", "short_put"):
            if strategy == "long_call":
                legs = (long_call(long_strike, quality.price),)
            elif strategy == "long_put":
                legs = (long_put(long_strike, quality.price),)
            elif strategy == "short_call":
                legs = (short_call(long_strike, quality.price),)
            else:
                legs = (short_put(long_strike, quality.price),)
            summary = summarize_expiry(legs)

            candidates.append(DirectionalCandidate(
                underlying=snapshot.underlying, strategy=strategy, expiry=expiry, dte=dte, legs=legs,
                net_debit=-summary.net_credit, debit_per_share=-summary.net_credit / 100,
                max_profit=summary.max_profit, max_loss=summary.max_loss, return_on_risk=None,
                breakevens=summary.breakevens, signal_context=rsi_signal_context(rsi, "iron_condor"),
                snapshot_id=snapshot.snapshot_id, warnings=tuple(quality.warnings),
            ))
        else:
            # Two-leg debit spread (bull call / bear put).
            short_q = _pick_short_for_debit(right, long_strike, by_strike, filt)
            if not short_q:
                continue
            short_strike = short_q.contract.strike
            s_quality = evaluate_quote_quality(short_q, min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct)
            if not s_quality.tradable or s_quality.price is None:
                continue

            try:
                legs = (bull_call_spread(long_strike, quality.price, short_strike, s_quality.price)
                        if right == "call"
                        else bear_put_spread(long_strike, quality.price, short_strike, s_quality.price))
                summary = summarize_expiry(legs)
            except (ValueError, ArithmeticError):
                continue
            debit = -summary.net_credit
            if debit <= 0:
                continue

            candidates.append(DirectionalCandidate(
                underlying=snapshot.underlying, strategy=strategy, expiry=expiry, dte=dte, legs=legs,
                net_debit=debit, debit_per_share=debit / 100, max_profit=summary.max_profit, max_loss=summary.max_loss,
                return_on_risk=summary.max_profit / summary.max_loss if summary.max_loss else None,
                breakevens=summary.breakevens, signal_context=rsi_signal_context(rsi, "iron_condor"),
                snapshot_id=snapshot.snapshot_id, warnings=tuple(dict.fromkeys((*quality.warnings, *s_quality.warnings))),
            ))

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


# --------------------------------------------------------------------------- #
# Wheel / LEAPS filters + income builders (reuse the directional engine)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WheelFilters:
    min_open_interest: int = 100
    max_spread_pct: Decimal = Decimal("0.15")
    min_dte: int = 30
    max_dte: int = 45
    short_delta_low: Decimal = Decimal("0.20")
    short_delta_high: Decimal = Decimal("0.30")

    def __post_init__(self) -> None:
        for name in ("max_spread_pct", "short_delta_low", "short_delta_high"):
            object.__setattr__(self, name, dec(getattr(self, name)))


@dataclass(frozen=True)
class WheelFiltersShortTerm(WheelFilters):
    min_dte: int = 7
    max_dte: int = 14


@dataclass(frozen=True)
class LeapsFilters:
    min_open_interest: int = 100
    max_spread_pct: Decimal = Decimal("0.15")
    min_dte: int = 365
    max_dte: int = 730
    long_delta_low: Decimal = Decimal("0.70")
    long_delta_high: Decimal = Decimal("0.90")

    def __post_init__(self) -> None:
        for name in ("max_spread_pct", "long_delta_low", "long_delta_high"):
            object.__setattr__(self, name, dec(getattr(self, name)))


def build_cash_secured_puts(snapshot, expiry, *, today=None, filters=None, rsi=None):
    filt = filters or WheelFilters()
    return _build_directional("short_put", snapshot, expiry, today=today, filters=DirectionalFilters(
        min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct,
        min_dte=filt.min_dte, max_dte=filt.max_dte,
        long_delta_low=filt.short_delta_low, long_delta_high=filt.short_delta_high,
    ), rsi=rsi)


def build_covered_calls(snapshot, expiry, *, today=None, filters=None, rsi=None):
    filt = filters or WheelFilters()
    return _build_directional("short_call", snapshot, expiry, today=today, filters=DirectionalFilters(
        min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct,
        min_dte=filt.min_dte, max_dte=filt.max_dte,
        long_delta_low=filt.short_delta_low, long_delta_high=filt.short_delta_high,
    ), rsi=rsi)


def build_pmcc_leaps(snapshot, expiry, *, today=None, filters=None, rsi=None):
    filt = filters or LeapsFilters()
    return _build_directional("long_call", snapshot, expiry, today=today, filters=DirectionalFilters(
        min_open_interest=filt.min_open_interest, max_spread_pct=filt.max_spread_pct,
        min_dte=filt.min_dte, max_dte=filt.max_dte,
        long_delta_low=filt.long_delta_low, long_delta_high=filt.long_delta_high,
    ), rsi=rsi)


@dataclass(frozen=True)
class PMCCCandidate:
    underlying: str
    leaps_expiry: date
    short_expiry: date
    leaps_strike: Decimal
    short_strike: Decimal
    leaps_premium: Decimal
    short_premium: Decimal
    net_debit: Decimal
    max_risk: Decimal
    approx_max_profit: Decimal
    leaps_delta: Decimal
    short_delta: Decimal


@dataclass(frozen=True)
class PMCCScan:
    underlying: str
    candidates: tuple[PMCCCandidate, ...] = ()
    warnings: tuple[str, ...] = ()


def build_full_pmcc(
    snapshot: OptionChainSnapshot,
    leaps_expiry: date,
    short_expiry: date,
    *,
    leaps_filters: LeapsFilters | None = None,
    short_filters: SpreadFilters | None = None,
) -> PMCCScan:
    """Pair a deep-ITM LEAPS long call with a nearer-dated short call (diagonal)."""
    l_filt = leaps_filters or LeapsFilters()
    s_filt = short_filters or SpreadFilters()

    leaps_quotes = [q for q in quotes_for_expiry(snapshot, leaps_expiry) if q.contract.right == "call"]
    short_quotes = [q for q in quotes_for_expiry(snapshot, short_expiry) if q.contract.right == "call"]

    candidates = []

    for lq in leaps_quotes:
        l_delta = abs(float(lq.delta)) if lq.delta else 0
        if not (float(l_filt.long_delta_low) <= l_delta <= float(l_filt.long_delta_high)):
            continue

        l_qual = evaluate_quote_quality(lq, min_open_interest=l_filt.min_open_interest, max_spread_pct=l_filt.max_spread_pct)
        if not l_qual.tradable or not l_qual.price:
            continue

        for sq in short_quotes:
            if sq.contract.strike <= lq.contract.strike:  # short call must sit ABOVE the LEAPS strike
                continue

            s_delta = abs(float(sq.delta)) if sq.delta else 0
            if not (float(s_filt.short_delta_low) <= s_delta <= float(s_filt.short_delta_high)):
                continue

            s_qual = evaluate_quote_quality(sq, min_open_interest=s_filt.min_open_interest, max_spread_pct=s_filt.max_spread_pct)
            if not s_qual.tradable or not s_qual.price:
                continue

            net_debit = l_qual.price - s_qual.price
            approx_max_profit = (sq.contract.strike - lq.contract.strike) - net_debit
            if approx_max_profit <= 0:
                continue

            candidates.append(PMCCCandidate(
                underlying=snapshot.underlying,
                leaps_expiry=leaps_expiry,
                short_expiry=short_expiry,
                leaps_strike=lq.contract.strike,
                short_strike=sq.contract.strike,
                leaps_premium=l_qual.price,
                short_premium=s_qual.price,
                net_debit=net_debit,
                max_risk=net_debit,
                approx_max_profit=approx_max_profit,
                leaps_delta=Decimal(str(round(l_delta, 2))),
                short_delta=Decimal(str(round(s_delta, 2))),
            ))

    candidates.sort(key=lambda c: c.net_debit)  # cheapest diagonal first
    return PMCCScan(snapshot.underlying, tuple(candidates))


# --------------------------------------------------------------------------- #
# CLI — fetch a snapshot per ticker and print the top candidate per structure.
# --------------------------------------------------------------------------- #
def _get_snapshot(client, ticker: str) -> OptionChainSnapshot | None:
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
        fi = getattr(tk, "fast_info", None)
        price = getattr(fi, "last_price", None)
        if not price:
            hist = tk.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if not price:
            return None

        exps = tk.options
        if not exps:
            return None

        target_exps = list(exps[:5])
        today = date.today()
        for exp in exps:
            exp_date = date.fromisoformat(exp.replace("/", "-"))
            if (exp_date - today).days > 365:
                if exp not in target_exps:
                    target_exps.append(exp)
                break

        quotes = []
        for exp in target_exps:
            chain = fetch_option_chain(ticker, exp, quote_client=client)
            for c in chain:
                right = str(c.get("right", c.get("option_type", c.get("type", "")))).lower()
                if right not in ("call", "put"):
                    continue

                strike = float(c.get("strike", c.get("strike_price", 0)))
                bid = float(c.get("bid_price", c.get("bid", 0)))
                ask = float(c.get("ask_price", c.get("ask", 0)))
                oi = int(c.get("open_interest", 0))
                delta = c.get("delta")
                if delta is not None:
                    delta = float(delta)

                contract = OptionContract(ticker, ticker, date.fromisoformat(exp.replace("/", "-")), strike, right)
                quotes.append(OptionQuote(contract, datetime.now(timezone.utc), "broker_live" if client else "delayed",
                                          bid=bid, ask=ask, last=None, volume=0, open_interest=oi,
                                          implied_volatility=None, delta=delta))

        return OptionChainSnapshot(
            snapshot_id=f"dir_bld_{ticker}_{date.today().isoformat()}",
            underlying=ticker,
            underlying_price=price,
            quotes=tuple(quotes),
            as_of=datetime.now(timezone.utc),
            source="broker_live" if client else "delayed",
        )
    except Exception as e:
        log.warning(f"Failed to build snapshot for {ticker}: {e}")
        return None


def _fmt(value) -> str:
    """Format an optional Decimal for the CLI (``n/a`` when unavailable)."""
    return "n/a" if value is None else f"{value:.2f}"


def main(argv: Sequence[str] | None = None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Directional Strategy Builder.")
    ap.add_argument("tickers", nargs="*", help="Watchlist ticker symbols.")
    args = ap.parse_args(argv)

    if not args.tickers:
        print("No tickers provided.")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = _build_quote_client()
    if not client:
        print("Tiger QuoteClient not available. Option chains might not be fetched optimally.")

    for ticker in args.tickers:
        print(f"\nScanning {ticker}...")
        snap = _get_snapshot(client, ticker)
        if not snap:
            print(f"  [!] Could not fetch data for {ticker}")
            continue

        print(f"  Spot Price: ${snap.underlying_price}")
        expiries = sorted({q.contract.expiry for q in snap.quotes})
        for exp in expiries:
            bcs = build_bull_call_spreads(snap, exp)
            if bcs.candidates:
                c = bcs.candidates[0]
                strikes = "/".join(str(l.strike) for l in c.legs)
                print(f"  [Bull Call] {exp} {strikes} | Debit: ${_fmt(c.net_debit)} | "
                      f"Max Loss: ${_fmt(c.max_loss)} | Max Profit: ${_fmt(c.max_profit)} | RoR: {_fmt(c.return_on_risk)}")

            bps = build_bear_put_spreads(snap, exp)
            if bps.candidates:
                c = bps.candidates[0]
                strikes = "/".join(str(l.strike) for l in c.legs)
                print(f"  [Bear Put]  {exp} {strikes} | Debit: ${_fmt(c.net_debit)} | "
                      f"Max Loss: ${_fmt(c.max_loss)} | Max Profit: ${_fmt(c.max_profit)} | RoR: {_fmt(c.return_on_risk)}")

            lc = build_long_calls(snap, exp)
            if lc.candidates:
                c = lc.candidates[0]
                be = _fmt(c.breakevens[0]) if c.breakevens else "n/a"
                print(f"  [Long Call] {exp} {c.legs[0].strike}C | Debit: ${_fmt(c.net_debit)} | "
                      f"Max Loss: ${_fmt(c.max_loss)} | Breakeven: ${be}")

            lp = build_long_puts(snap, exp)
            if lp.candidates:
                c = lp.candidates[0]
                be = _fmt(c.breakevens[0]) if c.breakevens else "n/a"
                print(f"  [Long Put]  {exp} {c.legs[0].strike}P | Debit: ${_fmt(c.net_debit)} | "
                      f"Max Loss: ${_fmt(c.max_loss)} | Breakeven: ${be}")

            csp = build_cash_secured_puts(snap, exp)
            if not csp.candidates:
                csp = build_cash_secured_puts(snap, exp, filters=WheelFiltersShortTerm())
            if csp.candidates:
                c = csp.candidates[0]
                print(f"  [CSP]       {exp} {c.legs[0].strike}P | Credit: ${_fmt(-c.net_debit)} | "
                      f"Capital Required: ${c.legs[0].strike * 100:.2f}")

            cc = build_covered_calls(snap, exp)
            if not cc.candidates:
                cc = build_covered_calls(snap, exp, filters=WheelFiltersShortTerm())
            if cc.candidates:
                c = cc.candidates[0]
                print(f"  [Cov Call]  {exp} {c.legs[0].strike}C | Credit: ${_fmt(-c.net_debit)}")

            leaps = build_pmcc_leaps(snap, exp)
            if leaps.candidates:
                c = leaps.candidates[0]
                print(f"  [PMCC Buy]  {exp} {c.legs[0].strike}C | Debit: ${_fmt(c.net_debit)} | Delta: >0.70")

        leaps_expiries = [e for e in expiries if (e - date.today()).days > 365]
        near_term_expiries = [e for e in expiries if 21 <= (e - date.today()).days <= 45]

        if leaps_expiries and near_term_expiries:
            pmcc = build_full_pmcc(snap, leaps_expiries[0], near_term_expiries[0])
            if pmcc.candidates:
                c = pmcc.candidates[0]
                print(f"  [PMCC Full] Buy {c.leaps_expiry} {c.leaps_strike}C / Sell {c.short_expiry} {c.short_strike}C | "
                      f"Debit: ${c.net_debit:.2f} | Approx Max Profit: ${c.approx_max_profit:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

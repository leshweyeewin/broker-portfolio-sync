"""Fixed-fractional position sizing — the "risk no more than X% of equity per
trade" calculator.

Pure arithmetic, no market data, no advice: you supply account equity, the risk
cap, and the trade's risk-per-unit; it returns how many shares/contracts keep the
loss inside that cap. Default cap is 2% (the common convention).

Money is ``Decimal`` throughout (§4) — every numeric input is coerced via
:func:`adapters.base.dec` so no float artifact leaks in. This is a calculator
only; it does not recommend a risk level or whether to take a trade.

Usage:
    python -m analytics.position_sizing --equity 25000 --entry 180 --stop 174
    python -m analytics.position_sizing --equity 30000 --max-loss-per-contract 350
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from adapters.base import dec

DEFAULT_RISK_PCT = Decimal("2")  # percent of equity risked per trade
ZERO = Decimal("0")


@dataclass
class SizeResult:
    units: int              # shares (equity) or contracts (options)
    equity: Decimal
    risk_pct: Decimal
    risk_budget: Decimal    # equity * risk_pct / 100
    risk_per_unit: Decimal  # $ at risk per share/contract
    actual_risk: Decimal    # units * risk_per_unit (<= risk_budget)
    notional: Decimal       # capital deployed (equity) or defined max loss (options)
    note: str = ""

    @property
    def actual_risk_pct(self) -> Optional[Decimal]:
        if self.equity <= 0:
            return None
        return (self.actual_risk / self.equity * 100).quantize(Decimal("0.001"))


def size_shares(equity, entry, stop, risk_pct: Decimal = DEFAULT_RISK_PCT) -> SizeResult:
    """Shares to buy/short so a stop-out loses <= risk_pct of equity.

    risk_per_share = |entry - stop|; shares = floor(budget / risk_per_share),
    then capped so notional never exceeds equity (cash, no margin assumed).
    """
    equity, entry, stop, risk_pct = dec(equity), dec(entry), dec(stop), dec(risk_pct)
    if equity <= 0:
        return _empty(equity, risk_pct, "equity must be positive")
    if entry <= 0:
        return _empty(equity, risk_pct, "entry price must be positive")
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return _empty(equity, risk_pct, "entry and stop are equal — no defined risk per share")

    budget = equity * risk_pct / 100
    shares = int(budget // risk_per_share)

    note = ""
    if shares == 0:
        note = ("Even 1 share risks more than the budget — raise equity, tighten "
                "the stop, or accept a smaller risk %.")
    elif shares * entry > equity:
        shares = int(equity // entry)
        note = f"Capped by cash: risk-based size exceeded equity, sized to {shares} affordable share(s)."

    return SizeResult(shares, equity, risk_pct, budget, risk_per_share,
                      shares * risk_per_share, shares * entry, note)


def size_contracts(equity, max_loss_per_contract,
                   risk_pct: Decimal = DEFAULT_RISK_PCT) -> SizeResult:
    """Option contracts so the defined max loss stays <= risk_pct of equity.

    Caller supplies max_loss_per_contract in DOLLARS, e.g.:
      * long option ............ premium * 100
      * debit / credit vertical  (width - credit) * 100   [defined risk]
      * cash-secured put ....... (strike - credit) * 100  [assignment risk]
    """
    equity, max_loss, risk_pct = dec(equity), dec(max_loss_per_contract), dec(risk_pct)
    if equity <= 0:
        return _empty(equity, risk_pct, "equity must be positive")
    if max_loss <= 0:
        return _empty(equity, risk_pct, "max_loss_per_contract must be positive")

    budget = equity * risk_pct / 100
    contracts = int(budget // max_loss)
    note = "" if contracts else (
        "Even 1 contract exceeds the risk budget — this trade is too large for the "
        "risk %, or the strategy's max loss is too high.")
    return SizeResult(contracts, equity, risk_pct, budget, max_loss,
                      contracts * max_loss, contracts * max_loss, note)


def _empty(equity: Decimal, risk_pct: Decimal, note: str) -> SizeResult:
    budget = (equity if equity > 0 else ZERO) * risk_pct / 100
    return SizeResult(0, equity, risk_pct, budget, ZERO, ZERO, ZERO, note)


def format_result(r: SizeResult) -> str:
    """Plain-text summary of a sizing result."""
    unit = "contract(s)" if r.notional == r.actual_risk and r.risk_per_unit else "share(s)"
    lines = [
        f"Size: {r.units} {unit}",
        f"Risk budget: ${r.risk_budget:.2f} ({r.risk_pct:g}% of ${r.equity:.2f})",
        f"Risk/unit: ${r.risk_per_unit:.2f}",
        f"Actual risk: ${r.actual_risk:.2f}"
        + (f" ({r.actual_risk_pct:g}% of equity)" if r.actual_risk_pct is not None else ""),
    ]
    if r.note:
        lines.append(f"Note: {r.note}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fixed-fractional position sizing (2% rule).")
    ap.add_argument("--equity", required=True, help="account equity (money)")
    ap.add_argument("--entry", help="equity trade entry price")
    ap.add_argument("--stop", help="equity trade stop price")
    ap.add_argument("--max-loss-per-contract", dest="max_loss",
                    help="option defined max loss per contract, in dollars")
    ap.add_argument("--risk-pct", default=str(DEFAULT_RISK_PCT), help="risk cap %% (default 2)")
    args = ap.parse_args(argv)

    if args.max_loss:
        r = size_contracts(args.equity, args.max_loss, dec(args.risk_pct))
    elif args.entry and args.stop:
        r = size_shares(args.equity, args.entry, args.stop, dec(args.risk_pct))
    else:
        ap.error("provide either --entry and --stop (equity) or --max-loss-per-contract (option)")
        return 2
    print(format_result(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

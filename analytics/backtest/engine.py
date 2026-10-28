"""Long-only, one-position-at-a-time stock backtest engine.

Timing: the signal is observed at the close of bar ``i``; the fill happens at
the open of bar ``i+1``.  Costs: a fixed fee per fill plus adverse slippage in
basis points.  A position still open on the last bar is closed at that bar's
close and flagged ``forced``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Sequence

from analytics.backtest.bars import Bar
from analytics.backtest.rules import RuleSpec

ZERO = Decimal("0")


@dataclass(frozen=True)
class Trade:
    entry_day: date
    exit_day: date
    shares: int
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal          # net of fees and slippage
    return_pct: Decimal   # pnl / cost of entry
    bars_held: int
    forced: bool = False  # closed at the end of data, not by the rule
    note: str = ""        # why it closed: rule/stop/target/expiry/forced (+ modelled_exit)


@dataclass(frozen=True)
class BacktestResult:
    rule: RuleSpec
    start_capital: Decimal
    trades: tuple[Trade, ...]
    equity: tuple[tuple[date, Decimal], ...]   # mark-to-market at each close
    bars_in_market: int
    first_day: date
    last_day: date
    pricing: str = "stock"          # stock | modelled | snapshot
    structure: str = "long stock"
    risk: str = ""                 # sizing text for the system sentence
    signals: int = 0                # entry signals seen
    skipped: tuple[tuple[str, int], ...] = ()   # (reason, count) for signals not traded


def run_backtest(
    bars: Sequence[Bar],
    rule: RuleSpec,
    *,
    capital: Decimal | str | int = 10000,
    fee_per_fill: Decimal | str | int = 0,
    slippage_bps: Decimal | str | int = 5,
) -> BacktestResult:
    if len(bars) < 2:
        raise ValueError("need at least two bars")
    capital = Decimal(str(capital))
    fee = Decimal(str(fee_per_fill))
    slip = Decimal(str(slippage_bps)) / Decimal("10000")

    cash = capital
    shares = 0
    entry_day = entry_fill = entry_cost = None
    entry_idx = 0
    trades: list[Trade] = []
    equity: list[tuple[date, Decimal]] = []
    in_market = 0

    def close_out(idx: int, price: Decimal, forced: bool) -> None:
        nonlocal cash, shares
        fill = price * (1 - slip)
        proceeds = fill * shares - fee
        pnl = proceeds - entry_cost
        cash += proceeds
        trades.append(Trade(entry_day, bars[idx].day, shares, entry_fill, fill, pnl,
                            pnl / entry_cost, idx - entry_idx, forced))
        shares = 0

    for i, bar in enumerate(bars):
        # 1) act on the signal observed at the previous close, at today's open
        if i > 0:
            if shares == 0 and rule.entry(bars, i - 1):
                fill = bar.open * (1 + slip)
                budget = (cash + ZERO) * rule.size_fraction
                qty = int(((budget - fee) / fill).to_integral_value(rounding=ROUND_DOWN))
                if qty > 0:
                    entry_fill, entry_day, entry_idx = fill, bar.day, i
                    entry_cost = fill * qty + fee
                    cash -= entry_cost
                    shares = qty
            elif shares > 0 and rule.exit(bars, i - 1):
                close_out(i, bar.open, False)
        if shares > 0:
            in_market += 1
        equity.append((bar.day, cash + bar.close * shares))

    if shares > 0:
        close_out(len(bars) - 1, bars[-1].close, True)
        equity[-1] = (bars[-1].day, cash)

    return BacktestResult(rule, capital, tuple(trades), tuple(equity), in_market,
                          bars[0].day, bars[-1].day)

"""Day 3 robustness checks: challenge a backtest before trusting it.

    python -m analytics.backtest.robust AAPL --rule mean_reversion

Checks: buy-and-hold benchmark, signals delayed by one session, a period x k
parameter grid with a neighbour view, and a train/test split where the grid
winner is picked on the first part only.  Stock engine; read-only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date, timedelta
from statistics import fmean
from typing import Callable, Sequence

from analytics.backtest.bars import Bar, load_csv
from analytics.backtest.data import fetch_bars
from analytics.backtest.engine import run_backtest
from analytics.backtest.metrics import compute_metrics
from analytics.backtest.rules import DEFAULT_RULES, RuleSpec

PERIODS = (10, 15, 20, 30, 40)
KS = (1.0, 1.5, 2.0, 2.5)
TRAIN_FRACTION = 0.7
FRAGILE_RATIO = 0.5   # neighbours averaging under half the best cell's Sharpe


@dataclass(frozen=True)
class Cell:
    period: int
    k: float
    sharpe: float | None
    return_pct: float
    trades: int


def buy_and_hold_pct(bars: Sequence[Bar]) -> float:
    return (float(bars[-1].close) / float(bars[0].open) - 1) * 100


def delayed(rule: RuleSpec, sessions: int = 1) -> RuleSpec:
    """Same rule, but every signal is acted on ``sessions`` bars later."""
    def lag(fn):
        return lambda bars, i: i - sessions >= 0 and fn(bars, i - sessions)
    return replace(rule, name=f"{rule.name}+delay{sessions}", entry=lag(rule.entry), exit=lag(rule.exit))


def _sharpe_key(c: Cell) -> float:
    return c.sharpe if c.sharpe is not None else float("-inf")


def grid(bars: Sequence[Bar], make_rule: Callable[..., RuleSpec], **run_kw) -> list[Cell]:
    cells = []
    for p in PERIODS:
        for k in KS:
            m = compute_metrics(run_backtest(bars, make_rule(period=p, k=k), **run_kw))
            cells.append(Cell(p, k, m.sharpe, m.total_return_pct, m.trades))
    return cells


def neighbours(cells: Sequence[Cell], best: Cell) -> list[Cell]:
    pi, ki = PERIODS.index(best.period), KS.index(best.k)
    return [c for c in cells if c is not best
            and abs(PERIODS.index(c.period) - pi) <= 1 and abs(KS.index(c.k) - ki) <= 1]


def is_fragile(cells: Sequence[Cell], best: Cell) -> bool:
    """True when the best cell is an island: neighbours are much weaker or losing."""
    near = [c.sharpe for c in neighbours(cells, best) if c.sharpe is not None]
    if best.sharpe is None or best.sharpe <= 0 or not near:
        return True
    return fmean(near) < FRAGILE_RATIO * best.sharpe


def split(bars: Sequence[Bar], fraction: float = TRAIN_FRACTION) -> tuple[Sequence[Bar], Sequence[Bar]]:
    cut = int(len(bars) * fraction)
    return bars[:cut], bars[cut:]


def _f(v, spec):
    return "n/a" if v is None else format(v, spec)


def report(bars: Sequence[Bar], make_rule: Callable[..., RuleSpec], *, period: int = 20, k: float = 2.0,
           **run_kw) -> str:
    base = run_backtest(bars, make_rule(period=period, k=k), **run_kw)
    bm = compute_metrics(base)
    lines = [f"Robustness: {base.rule.name}  {bars[0].day} -> {bars[-1].day}",
             f"Base          {bm.total_return_pct:+.1f}%  Sharpe {_f(bm.sharpe, '.2f')}  "
             f"maxDD -{bm.max_drawdown_pct:.1f}%  {bm.trades} trades  "
             f"PF {_f(bm.profit_factor, '.2f')}  exp {_f(bm.expectancy, ',.2f')}"]
    hold = buy_and_hold_pct(bars)
    lines.append(f"Benchmark     buy & hold {hold:+.1f}% -> rule {'beats' if bm.total_return_pct > hold else 'LAGS'} "
                 f"doing nothing (exposure {bm.exposure_pct:.0f}%)")
    dm = compute_metrics(run_backtest(bars, delayed(make_rule(period=period, k=k)), **run_kw))
    lines.append(f"Delay 1 bar   {dm.total_return_pct:+.1f}%  Sharpe {_f(dm.sharpe, '.2f')} "
                 f"-> {'holds up' if dm.total_return_pct > 0 else 'EDGE DISAPPEARS'}")

    cells = grid(bars, make_rule, **run_kw)
    best = max(cells, key=_sharpe_key)
    lines.append(f"Grid best     period {best.period} / k {best.k:g}: Sharpe {_f(best.sharpe, '.2f')}, "
                 f"{best.return_pct:+.1f}%, {best.trades} trades")
    near = [c.sharpe for c in neighbours(cells, best) if c.sharpe is not None]
    lines.append(f"Neighbours    avg Sharpe {_f(fmean(near) if near else None, '.2f')} over {len(near)} cells "
                 f"-> {'FRAGILE (island)' if is_fragile(cells, best) else 'stable plateau'}")
    lines.append(f"Grid overall  {sum(1 for c in cells if c.return_pct > 0)}/{len(cells)} settings profitable")

    train, test = split(bars)
    tbest = max(grid(train, make_rule, **run_kw), key=_sharpe_key)
    tm = compute_metrics(run_backtest(test, make_rule(period=tbest.period, k=tbest.k), **run_kw))
    lines.append(f"Train/test    picked period {tbest.period} / k {tbest.k:g} on first {TRAIN_FRACTION:.0%} "
                 f"(Sharpe {_f(tbest.sharpe, '.2f')}); unseen from {test[0].day}: {tm.total_return_pct:+.1f}%, "
                 f"Sharpe {_f(tm.sharpe, '.2f')}, {tm.trades} trades")
    lines.append("Not covered: regime split, fee stress, other tickers. Backtests expose assumptions; "
                 "they do not prove future results.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Robustness checks for a stock rule backtest (read-only).")
    ap.add_argument("source", help="CSV path or ticker")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--rule", choices=["mean_reversion", "trend"], default="mean_reversion")
    ap.add_argument("--period", type=int, default=20)
    ap.add_argument("--k", type=float, default=2.0)
    ap.add_argument("--capital", default="10000")
    ap.add_argument("--fee", default="0")
    ap.add_argument("--slippage-bps", default="5")
    a = ap.parse_args(argv)
    if a.source.lower().endswith(".csv"):
        bars = load_csv(a.source)
    else:
        end = date.today()
        bars, used = fetch_bars(a.source, end - timedelta(days=int(a.years * 365)), end, ("longbridge", "yfinance"))
        print(f"Fetched {len(bars)} bars for {a.source.upper()} via {used}")
    print(report(bars, DEFAULT_RULES[a.rule], period=a.period, k=a.k,
                 capital=a.capital, fee_per_fill=a.fee, slippage_bps=a.slippage_bps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Metrics, meant to be read together (return + drawdown + win/loss size +
trade count + holding period), never the win rate alone."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import fmean, pstdev

from analytics.backtest.engine import BacktestResult

MIN_TRADES = 30
TRADING_DAYS = 252


@dataclass(frozen=True)
class Metrics:
    total_return_pct: float
    sharpe: float | None
    max_drawdown_pct: float
    win_rate: float | None
    avg_gain: float | None
    avg_loss: float | None
    wins_erased_by_one_loss: float | None   # avg loss / avg gain
    profit_factor: float | None             # gross gains / gross losses
    expectancy: float | None                # average net P/L per trade
    trades: int
    avg_bars_held: float | None
    exposure_pct: float
    warnings: tuple[str, ...]


def compute_metrics(result: BacktestResult) -> Metrics:
    eq = [float(v) for _, v in result.equity]
    start = float(result.start_capital)
    rets = [b / a - 1 for a, b in zip(eq, eq[1:]) if a]
    sd = pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = fmean(rets) / sd * sqrt(TRADING_DAYS) if sd else None

    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak else 0.0)

    wins = [float(t.pnl) for t in result.trades if t.pnl > 0]
    losses = [-float(t.pnl) for t in result.trades if t.pnl < 0]
    n = len(result.trades)
    avg_gain = fmean(wins) if wins else None
    avg_loss = fmean(losses) if losses else None
    erased = avg_loss / avg_gain if avg_gain and avg_loss else None
    profit_factor = sum(wins) / sum(losses) if losses else None
    expectancy = fmean(float(t.pnl) for t in result.trades) if result.trades else None

    warnings = []
    if n < MIN_TRADES:
        warnings.append(f"only {n} trades (<{MIN_TRADES}): treat results as noise, not evidence")
    if any(t.forced for t in result.trades):
        warnings.append("last position was force-closed at the end of the data")
    skipped = sum(c for _, c in result.skipped)
    if skipped:
        detail = ", ".join(f"{r}: {c}" for r, c in result.skipped)
        warnings.append(f"{skipped} of {result.signals} signals not traded ({detail})")
    if result.pricing == "modelled":
        warnings.append("modelled option pricing: no skew, no real bid/ask - approximate, compare rules not dollars")
    if any("modelled_exit" in t.note for t in result.trades) and result.pricing == "snapshot":
        warnings.append("some exits were priced by the model (no snapshot on that day)")
    if erased is not None and erased > 1:
        warnings.append(f"one average loss erases {erased:.1f} average wins")

    return Metrics(
        total_return_pct=(eq[-1] / start - 1) * 100,
        sharpe=sharpe,
        max_drawdown_pct=mdd * 100,
        win_rate=len(wins) / n if n else None,
        avg_gain=avg_gain, avg_loss=avg_loss, wins_erased_by_one_loss=erased,
        profit_factor=profit_factor, expectancy=expectancy,
        trades=n,
        avg_bars_held=fmean(t.bars_held for t in result.trades) if n else None,
        exposure_pct=result.bars_in_market / len(eq) * 100,
        warnings=tuple(warnings),
    )


def format_report(result: BacktestResult, m: Metrics) -> str:
    f = lambda v, spec: "n/a" if v is None else format(v, spec)
    lines = [
        f"Backtest: {result.rule.name} v{result.rule.version}  ({result.pricing} pricing, {result.structure})",
        f"Sample: {result.first_day} -> {result.last_day}, {len(result.equity)} bars, {m.trades} trades",
        result.rule.system_sentence(result.structure, result.risk or None),
        "",
        f"Return        {m.total_return_pct:+.1f}%",
        f"Sharpe        {f(m.sharpe, '.2f')}",
        f"Max drawdown  -{m.max_drawdown_pct:.1f}%",
        f"Win rate      {f(m.win_rate and m.win_rate * 100, '.0f')}%",
        f"Avg gain/loss {f(m.avg_gain, ',.2f')} / {f(m.avg_loss, ',.2f')}",
        f"Loss erases   {f(m.wins_erased_by_one_loss, '.1f')} avg wins",
        f"Profit factor {f(m.profit_factor, '.2f')}   Expectancy {f(m.expectancy, ',.2f')} per trade",
        f"Avg hold      {f(m.avg_bars_held, '.1f')} bars   Exposure {m.exposure_pct:.0f}%",
    ]
    lines += [f"WARNING: {w}" for w in m.warnings]
    lines.append("Backtests expose assumptions and trade-offs; they do not prove future results.")
    return "\n".join(lines)


def format_compact(result: BacktestResult, m: Metrics) -> str:
    """Two-line summary for chat/alerts; the full ``format_report`` stays for the CLI."""
    win = "n/a" if m.win_rate is None else f"{m.win_rate * 100:.0f}%"
    erase = "" if m.wins_erased_by_one_loss is None else f", 1 loss = {m.wins_erased_by_one_loss:.1f} wins"
    flag = " ⚠️ small sample" if m.trades < MIN_TRADES else ""
    return (f"{result.rule.name} · {result.pricing} {result.structure}\n"
            f"   {m.trades} trades, {win} win{erase}, {m.total_return_pct:+.1f}% / maxDD -{m.max_drawdown_pct:.1f}%{flag}")

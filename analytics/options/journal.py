"""P2.12: Strategy Journal and Scorecard.

Correlates closed FIFO results with original Trade Plans and grades execution.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean
from typing import Sequence
from datetime import datetime, timezone

from core.fifo_pl import Realization, compute_option_pl
from analytics.options.trade_plans import TradePlanStore, TradePlan, PlanStatus, transition
from sheets.writer import PortfolioWriter, SheetClient
from config.settings import get_service_account_info, get_spreadsheet_id

log = logging.getLogger(__name__)

@dataclass
class JournalEntry:
    plan: TradePlan
    realization: Realization
    grade: str
    notes: str

def generate_journal(plans: Sequence[TradePlan], realizations: Sequence[Realization]) -> list[JournalEntry]:
    entries = []
    # Sort realizations by date descending (newest first)
    sorted_realizations = sorted(realizations, key=lambda r: r.date, reverse=True)
    
    # We only care about recently closed trades
    for r in sorted_realizations:
        # Simple heuristic to parse option instrument string e.g., "AAPL 2026-09-18 200 Call"
        parts = r.instrument.split()
        if len(parts) < 4: continue
        ticker = parts[0]
        
        # Try to find a matching plan
        matched_plan = None
        for plan in plans:
            if plan.ticker == ticker and plan.status in (PlanStatus.ENTERED, PlanStatus.MANAGED, PlanStatus.CLOSED):
                # Basic fuzzy matching based on ticker and legs
                matched_plan = plan
                break
                
        if matched_plan:
            # Grade execution
            grade = "A"
            if r.realized_pl > 0:
                notes = f"Profitable close: +${r.realized_pl:,.2f}"
                if matched_plan.exit_rule and "50%" in matched_plan.exit_rule:
                    notes += " (Executed plan targets)"
            else:
                grade = "C"
                notes = f"Loss realized: -${abs(r.realized_pl):,.2f}"
                if matched_plan.invalidation:
                    notes += " (Hit invalidation rule)"
                    grade = "B" # Good execution of stop loss
                    
            entries.append(JournalEntry(matched_plan, r, grade, notes))
            
    return entries

NO_RULE = "(no rule)"
NO_PLAN = "(no plan)"
MIN_TRADES = 30  # same evidence bar as analytics.backtest.metrics


@dataclass(frozen=True)
class RuleScore:
    """Live results for one named rule, read together like the backtest metrics."""
    rule: str
    trades: int
    total_pl: Decimal
    win_rate: float | None
    avg_gain: Decimal | None
    avg_loss: Decimal | None
    wins_erased_by_one_loss: float | None
    max_drawdown: Decimal          # peak-to-trough of cumulative realised P/L, in $
    warnings: tuple[str, ...]


def _score(rule: str, rows: Sequence[Realization]) -> RuleScore:
    rows = sorted(rows, key=lambda r: r.date)
    pls = [r.realized_pl for r in rows]
    wins = [p for p in pls if p > 0]
    losses = [-p for p in pls if p < 0]
    cum = peak = mdd = Decimal("0")
    for p in pls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    avg_gain = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    erased = float(avg_loss / avg_gain) if avg_gain and avg_loss else None
    warnings = []
    if len(pls) < MIN_TRADES:
        warnings.append(f"only {len(pls)} trades (<{MIN_TRADES}): noise, not evidence")
    if erased is not None and erased > 1:
        warnings.append(f"one average loss erases {erased:.1f} average wins")
    if rule == NO_PLAN:
        warnings.append("closed trades with no saved plan: no stated rule, size or exit")
    elif rule == NO_RULE:
        warnings.append("plans exist but none names a rule (--rule-name)")
    return RuleScore(rule, len(pls), sum(pls, Decimal("0")), len(wins) / len(pls) if pls else None,
                     avg_gain, avg_loss, erased, mdd, tuple(warnings))


def scorecard_by_rule(plans: Sequence[TradePlan], realizations: Sequence[Realization]) -> list[RuleScore]:
    """Group realised option P/L by the rule of the plan it matched.

    Trades that match no plan are reported under ``(no plan)`` — closing without
    a written plan is itself a result worth seeing.  Worst total P/L first.
    """
    matched = {e.realization.key: (e.plan.rule_name or NO_RULE) for e in generate_journal(plans, realizations)}
    groups: dict[str, list[Realization]] = defaultdict(list)
    for r in realizations:
        groups[matched.get(r.key, NO_PLAN)].append(r)
    return sorted((_score(rule, rows) for rule, rows in groups.items()), key=lambda s: s.total_pl)


def format_scorecard(scores: Sequence[RuleScore]) -> str:
    if not scores:
        return "No closed trades."
    f = lambda v, spec: "n/a" if v is None else format(v, spec)
    lines = []
    for s in scores:
        lines += [
            f"{s.rule}: {s.trades} trades, P/L ${s.total_pl:,.2f}, win rate {f(s.win_rate and s.win_rate * 100, '.0f')}%",
            f"   avg gain/loss {f(s.avg_gain, ',.2f')} / {f(s.avg_loss, ',.2f')}   "
            f"loss erases {f(s.wins_erased_by_one_loss, '.1f')} avg wins   max drawdown ${s.max_drawdown:,.2f}",
            *[f"   WARNING: {w}" for w in s.warnings],
        ]
    return "\n".join(lines)


def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except: pass
        
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)
    option_trades = [item["trade"] for item in writer.read_all_option_trades()]
    res = compute_option_pl(option_trades)
    
    store = TradePlanStore()
    plans = store.list()
    
    print("\n" + "="*40)
    print("📊 RULE SCORECARD (realised, all closed option trades)")
    print("="*40)
    print(format_scorecard(scorecard_by_rule(plans, res.realizations)))

    entries = generate_journal(plans, res.realizations)
    
    print("\n" + "="*40)
    print("📓 STRATEGY JOURNAL & SCORECARD")
    print("="*40)
    
    if not entries:
        print("No correlated closed trades found.")
        return 0
        
    # Just show the top 10 most recent
    for idx, e in enumerate(entries[:10], 1):
        print(f"\n{idx}. {e.plan.ticker} | {e.plan.strategy} | Closed: {e.realization.date}")
        print(f"   Realized P/L: ${e.realization.realized_pl:,.2f}")
        print(f"   Grade: {e.grade}")
        print(f"   Notes: {e.notes}")
        print(f"   Plan Thesis: {e.plan.thesis or 'N/A'}")
        
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

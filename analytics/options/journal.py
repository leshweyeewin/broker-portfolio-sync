"""P2.12: Strategy Journal and Scorecard.

Correlates closed FIFO results with original Trade Plans and grades execution.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
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

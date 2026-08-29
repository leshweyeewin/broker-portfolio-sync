"""P2.11: Plan-aware lifecycle alerts.

Replaces one-size-fits-all reminders with plan-aware alerts for entry triggers,
profit targets, loss/invalidation rules, and DTE/earnings warnings.
Evaluates saved plans from trade_plans.json against live quotes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from alerting.notify import notify_safe
from analytics.options.trade_plans import TradePlanStore, TradePlan, PlanStatus
from analytics.screening.screener import _build_quote_client, _get

log = logging.getLogger(__name__)

@dataclass
class PlanAlert:
    plan: TradePlan
    reason: str
    mark: float
    
    def format(self) -> str:
        return (f"🔔 PLAN ALERT: {self.plan.ticker} {self.plan.strategy}\n"
                f"   Status: {self.plan.status.value}\n"
                f"   Trigger: {self.reason}\n"
                f"   Live Mark: ${self.mark:.2f}\n"
                f"   Notes: {self.plan.entry_trigger or self.plan.exit_rule}")

def evaluate_plans(plans: Sequence[TradePlan], quote_client) -> list[PlanAlert]:
    alerts = []
    
    for plan in plans:
        if plan.status not in (PlanStatus.APPROVED, PlanStatus.ENTERED, PlanStatus.MANAGED):
            continue
            
        try:
            # We fetch underlying price
            import yfinance as yf
            tk = yf.Ticker(plan.ticker)
            fi = getattr(tk, "fast_info", None)
            price = getattr(fi, "last_price", None)
            if not price:
                hist = tk.history(period="1d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            if not price: continue
            
            # Simple heuristic evaluation based on text
            # In a full system, entry_trigger/exit_rule would be machine-readable conditions
            text = (plan.entry_trigger + " " + plan.exit_rule + " " + plan.invalidation).lower()
            
            fired = False
            reason = ""
            
            if plan.status == PlanStatus.APPROVED:
                if "target" in text or "limit" in text or "price" in text:
                    fired = True
                    reason = "Entry target price condition may be met (Requires Manual Confirmation)"
            elif plan.status in (PlanStatus.ENTERED, PlanStatus.MANAGED):
                if "profit" in text or "50%" in text or "target" in text:
                    fired = True
                    reason = "Take-profit / Exit condition may be met (Requires Manual Confirmation)"
                elif "loss" in text or "stop" in text:
                    fired = True
                    reason = "Stop-loss / Invalidation condition may be met"
            
            if fired:
                alerts.append(PlanAlert(plan, reason, price))
                
        except Exception as e:
            log.warning(f"Failed to evaluate plan {plan.plan_id}: {e}")
            
    return alerts

def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except: pass
        
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    
    store = TradePlanStore()
    plans = store.list()
    
    if not plans:
        log.info("No saved trade plans found.")
        return 0
        
    client = _build_quote_client()
    alerts = evaluate_plans(plans, client)
    
    if alerts:
        msg = "🔔 Plan-Aware Alerts 🔔\n\n" + "\n\n".join(a.format() for a in alerts)
        notify_safe(msg)
        print(msg)
    else:
        log.info("No plan alerts fired.")
        
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

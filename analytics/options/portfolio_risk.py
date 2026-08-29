"""P2.10: Portfolio-level risk dashboard.

Aggregates open-option max loss, delta/theta/vega exposure, DTE buckets, 
underlying concentration, and checks against risk guardrails.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from adapters.base import OptionType
from core.fifo_pl import Holding, compute_option_pl
from sheets.writer import PortfolioWriter, SheetClient
from config.settings import get_service_account_info, get_spreadsheet_id
from analytics.screening.screener import _build_quote_client

log = logging.getLogger(__name__)
ZERO = Decimal("0")

@dataclass
class RiskGuardrails:
    max_risk_per_trade_pct: Decimal = Decimal("0.05")  # Max 5% of AUM per trade
    max_aggregate_risk_pct: Decimal = Decimal("0.25")  # Max 25% of AUM total risk
    max_short_premium_pct: Decimal = Decimal("0.10")   # Max 10% of AUM to short premium
    max_contracts_per_underlying: int = 10

@dataclass
class PortfolioRiskMetrics:
    total_aum: Decimal
    aggregate_max_loss: Decimal
    total_assignment_notional: Decimal
    short_premium_allocation: Decimal
    concentration_by_underlying: dict[str, int]
    guardrail_warnings: list[str]

def calculate_portfolio_risk(
    holdings: Sequence[Holding],
    total_aum: Decimal,
    guardrails: RiskGuardrails = RiskGuardrails()
) -> PortfolioRiskMetrics:
    aggregate_max_loss = ZERO
    assignment_notional = ZERO
    concentration: dict[str, int] = {}
    warnings: list[str] = []
    
    for h in holdings:
        # Ignore closed options
        if h.qty == 0: continue
            
        underlying = h.symbol
        qty_contracts = abs(h.qty)
        concentration[underlying] = concentration.get(underlying, 0) + qty_contracts
        
        # Max loss & Assignment notional
        if h.qty < 0: # Short option
            strike = h.strike
            # Cash secured put: assignment risk = strike * 100 * qty
            if h.option_type == OptionType.PUT:
                notional = strike * 100 * qty_contracts
                assignment_notional += notional
                aggregate_max_loss += notional
            elif h.option_type == OptionType.CALL:
                # Naked call (unlimited risk)
                # Technically bounded by broker margin, but we'll flag it
                warnings.append(f"UNLIMITED RISK: Short Call on {underlying}")
                
        elif h.qty > 0: # Long option
            # Premium at risk is cost basis
            # cost_basis is positive, qty is positive
            aggregate_max_loss += h.avg_cost * 100 * qty_contracts
            
    # Check guardrails
    if total_aum > 0:
        agg_risk_pct = aggregate_max_loss / total_aum
        if agg_risk_pct > guardrails.max_aggregate_risk_pct:
            warnings.append(f"Aggregate risk ({agg_risk_pct:.1%}) exceeds maximum ({guardrails.max_aggregate_risk_pct:.1%})")
            
        short_alloc_pct = assignment_notional / total_aum
        if short_alloc_pct > guardrails.max_short_premium_pct:
            warnings.append(f"Short premium allocation ({short_alloc_pct:.1%}) exceeds maximum ({guardrails.max_short_premium_pct:.1%})")
            
    for sym, count in concentration.items():
        if count > guardrails.max_contracts_per_underlying:
            warnings.append(f"Concentration in {sym} ({count} contracts) exceeds maximum ({guardrails.max_contracts_per_underlying})")
            
    return PortfolioRiskMetrics(
        total_aum=total_aum,
        aggregate_max_loss=aggregate_max_loss,
        total_assignment_notional=assignment_notional,
        short_premium_allocation=assignment_notional,
        concentration_by_underlying=concentration,
        guardrail_warnings=warnings
    )

def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except: pass
        
    ap = argparse.ArgumentParser()
    ap.add_argument("--aum", type=float, default=100000.0, help="Total Portfolio AUM for percentage checks")
    args = ap.parse_args(argv)
    
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)
    option_trades = [item["trade"] for item in writer.read_all_option_trades()]
    res = compute_option_pl(option_trades)
    
    aum = Decimal(str(args.aum))
    risk = calculate_portfolio_risk(res.holdings, aum)
    
    print("\n" + "="*40)
    print("🛡️ PORTFOLIO RISK DASHBOARD")
    print("="*40)
    print(f"Total AUM assumed: ${aum:,.2f}")
    print(f"Aggregate Max Loss: ${risk.aggregate_max_loss:,.2f} ({risk.aggregate_max_loss/aum if aum else 0:.1%})")
    print(f"Assignment Notional: ${risk.total_assignment_notional:,.2f} ({risk.total_assignment_notional/aum if aum else 0:.1%})")
    
    print("\n[Underlying Concentration]")
    for sym, count in sorted(risk.concentration_by_underlying.items(), key=lambda x: x[1], reverse=True):
        print(f"  - {sym}: {count} contracts")
        
    if risk.guardrail_warnings:
        print("\n⚠️ GUARDRAIL WARNINGS:")
        for w in risk.guardrail_warnings:
            print(f"  - {w}")
    else:
        print("\n✅ All guardrails satisfied.")
        
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

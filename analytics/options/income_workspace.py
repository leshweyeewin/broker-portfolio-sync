"""Tool #5: Income Strategy Workspace.

Workflow for covered calls, cash-secured puts, the Wheel, and PMCC.
Detects eligible shares/cash from the synced portfolio, queries option chains,
and outputs candidates.

Run: python -m analytics.income_workspace [--cash 10000] [TICKERS...]
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from adapters.base import OptionType, StockTrade, OptionTrade
from core.fifo_pl import Holding, compute_stock_pl, compute_option_pl
from sheets.writer import PortfolioWriter, SheetClient
from config.settings import get_service_account_info, get_spreadsheet_id
from analytics.screener import _build_quote_client, _get

log = logging.getLogger(__name__)
ZERO = Decimal("0")

@dataclass
class WheelState:
    ticker: str
    state: str  # "CASH", "SHORT_PUT", "SHARES", "COVERED_CALL", "PMCC_BASE", "PMCC_ACTIVE"
    qty: int
    cost_basis: Decimal
    note: str = ""

@dataclass
class CC_Candidate:
    ticker: str
    shares_held: int
    cost_basis: Decimal
    current_price: float
    strike: Decimal
    expiry: str
    premium: Decimal
    annualized_yield: float
    call_away_price: Decimal

@dataclass
class CSP_Candidate:
    ticker: str
    current_price: float
    strike: Decimal
    expiry: str
    premium: Decimal
    return_on_risk: float
    annualized_yield: float
    effective_cost: Decimal
    delta: float

@dataclass
class PMCC_Candidate:
    ticker: str
    long_strike: Decimal
    long_expiry: str
    short_strike: Decimal
    short_expiry: str
    premium: Decimal
    diagonal_risk: Decimal

def get_current_price(ticker: str) -> float:
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
        fi = getattr(tk, "fast_info", None)
        p = getattr(fi, "last_price", None)
        if p: return p
        hist = tk.history(period="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return 0.0

def fetch_chains_for_expiry(client, ticker: str, expiry: str) -> list[dict]:
    try:
        chain = client.get_option_chain(symbol=ticker, expiry=expiry, market="US")
        if chain is None: return []
        if hasattr(chain, "to_dict"): return chain.to_dict("records")
        if isinstance(chain, list): return chain
    except Exception:
        pass
    return []

def get_dte(expiry_str: str) -> int:
    try:
        # Assuming YYYY-MM-DD
        exp = date.fromisoformat(expiry_str.replace("/", "-"))
        return max(1, (exp - date.today()).days)
    except:
        return 30

def scan_csps(client, tickers: list[str], available_cash: float) -> list[CSP_Candidate]:
    candidates = []
    for ticker in tickers:
        price = get_current_price(ticker)
        if not price: continue
        
        # We need expirations. We can fetch briefs to get expirations.
        # But to keep it simple, if QuoteClient isn't providing briefs easily, we can use yfinance for expirations.
        try:
            import yfinance as yf
            tk = yf.Ticker(ticker)
            exps = tk.options
            if not exps: continue
            # Look for 14-45 DTE
            target_exps = [e for e in exps if 14 <= get_dte(e) <= 45]
            if not target_exps:
                target_exps = exps[:2]
        except Exception:
            continue
            
        for exp in target_exps[:2]:
            chain = fetch_chains_for_expiry(client, ticker, exp)
            for c in chain:
                right = str(_get(c, "right", "option_type", "type", "put_call", default="")).upper()
                if right not in ("PUT", "P"): continue
                
                strike = Decimal(str(_get(c, "strike", "strike_price", default=0)))
                if strike >= Decimal(str(price)): continue # OTM only
                
                bid = Decimal(str(_get(c, "bid_price", "bid", default=0)))
                ask = Decimal(str(_get(c, "ask_price", "ask", default=0)))
                mid = (bid + ask) / 2
                if mid <= 0: continue
                
                delta = abs(float(_get(c, "delta", default=0)))
                # Only look at delta < 0.35
                if delta > 0.35 and delta != 0: continue
                
                dte = get_dte(exp)
                ret_on_risk = float(mid / strike)
                ann_yield = ret_on_risk * (365 / dte)
                
                # Check cash
                if float(strike) * 100 > available_cash: continue
                
                candidates.append(CSP_Candidate(
                    ticker=ticker,
                    current_price=price,
                    strike=strike,
                    expiry=exp,
                    premium=mid,
                    return_on_risk=ret_on_risk,
                    annualized_yield=ann_yield,
                    effective_cost=strike - mid,
                    delta=delta
                ))
    # Sort by annualized yield
    candidates.sort(key=lambda x: x.annualized_yield, reverse=True)
    return candidates

def scan_covered_calls(client, states: list[WheelState]) -> list[CC_Candidate]:
    candidates = []
    uncovered = [s for s in states if s.state == "SHARES"]
    for s in uncovered:
        price = get_current_price(s.ticker)
        if not price: continue
        try:
            import yfinance as yf
            tk = yf.Ticker(s.ticker)
            exps = tk.options
            if not exps: continue
            target_exps = [e for e in exps if 14 <= get_dte(e) <= 45]
            if not target_exps: target_exps = exps[:2]
        except Exception: continue
        
        for exp in target_exps[:2]:
            chain = fetch_chains_for_expiry(client, s.ticker, exp)
            for c in chain:
                right = str(_get(c, "right", "option_type", "type", "put_call", default="")).upper()
                if right not in ("CALL", "C"): continue
                
                strike = Decimal(str(_get(c, "strike", "strike_price", default=0)))
                if strike <= Decimal(str(price)): continue # OTM only
                
                bid = Decimal(str(_get(c, "bid_price", "bid", default=0)))
                ask = Decimal(str(_get(c, "ask_price", "ask", default=0)))
                mid = (bid + ask) / 2
                if mid <= 0: continue
                
                dte = get_dte(exp)
                ret_on_risk = float(mid / s.cost_basis) if s.cost_basis > 0 else 0
                ann_yield = ret_on_risk * (365 / dte)
                
                candidates.append(CC_Candidate(
                    ticker=s.ticker,
                    shares_held=s.qty,
                    cost_basis=s.cost_basis,
                    current_price=price,
                    strike=strike,
                    expiry=exp,
                    premium=mid,
                    annualized_yield=ann_yield,
                    call_away_price=strike + mid
                ))
    candidates.sort(key=lambda x: x.annualized_yield, reverse=True)
    return candidates

def scan_pmccs(client, states: list[WheelState]) -> list[PMCC_Candidate]:
    candidates = []
    bases = [s for s in states if s.state == "PMCC_BASE"]
    for b in bases:
        price = get_current_price(b.ticker)
        if not price: continue
        
        parts = b.note.split()
        if len(parts) >= 5:
            l_strike = Decimal(parts[2])
            l_expiry = parts[4]
        else: continue
        
        try:
            import yfinance as yf
            tk = yf.Ticker(b.ticker)
            exps = tk.options
            if not exps: continue
            target_exps = [e for e in exps if 14 <= get_dte(e) <= 45]
            if not target_exps: target_exps = exps[:2]
        except Exception: continue
        
        for exp in target_exps[:2]:
            chain = fetch_chains_for_expiry(client, b.ticker, exp)
            for c in chain:
                right = str(_get(c, "right", "option_type", "type", "put_call", default="")).upper()
                if right not in ("CALL", "C"): continue
                
                strike = Decimal(str(_get(c, "strike", "strike_price", default=0)))
                if strike <= Decimal(str(price)): continue # OTM only
                
                bid = Decimal(str(_get(c, "bid_price", "bid", default=0)))
                ask = Decimal(str(_get(c, "ask_price", "ask", default=0)))
                mid = (bid + ask) / 2
                if mid <= 0: continue
                
                diag_risk = b.cost_basis - (strike - l_strike) - mid
                diag_risk_val = diag_risk if diag_risk > 0 else ZERO
                    
                candidates.append(PMCC_Candidate(
                    ticker=b.ticker,
                    long_strike=l_strike,
                    long_expiry=l_expiry,
                    short_strike=strike,
                    short_expiry=exp,
                    premium=mid,
                    diagonal_risk=diag_risk_val
                ))
    candidates.sort(key=lambda x: x.diagonal_risk)
    return candidates

def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
            
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*", help="Tickers to scan for income setups")
    ap.add_argument("--cash", type=float, default=10000.0, help="Available cash for CSPs")
    args = ap.parse_args(argv)
    
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)
    
    stock_trades = [item["trade"] for item in writer.read_all_stock_trades()]
    option_trades = [item["trade"] for item in writer.read_all_option_trades()]
    
    stock_res = compute_stock_pl(stock_trades)
    option_res = compute_option_pl(option_trades)
    
    # Evaluate Portfolio State (Wheel)
    states = []
    
    # 1. Shares
    for h in stock_res.holdings:
        if h.qty >= 100:
            states.append(WheelState(h.symbol, "SHARES", int(h.qty), h.avg_cost))
            
    # 2. Options
    for h in option_res.holdings:
        if h.qty < 0 and h.option_type == OptionType.PUT:
            states.append(WheelState(h.symbol, "SHORT_PUT", int(abs(h.qty)*100), h.avg_cost))
        elif h.qty < 0 and h.option_type == OptionType.CALL:
            # Check if covered
            covered = False
            for s in states:
                if s.ticker == h.symbol and s.state == "SHARES" and s.qty >= abs(h.qty)*100:
                    s.state = "COVERED_CALL"
                    covered = True
                    s.note = f"Covered by Short Call {h.strike} exp {h.expiry}"
            if not covered:
                states.append(WheelState(h.symbol, "NAKED_CALL", int(abs(h.qty)*100), h.avg_cost))
        elif h.qty > 0 and h.option_type == OptionType.CALL:
            if h.expiry and (h.expiry - date.today()).days >= 180:
                states.append(WheelState(h.symbol, "PMCC_BASE", int(h.qty*100), h.avg_cost, f"Long Call {h.strike} exp {h.expiry}"))
                
    print("\n" + "="*40)
    print("💼 WHEEL / PORTFOLIO STATES")
    print("="*40)
    if not states:
        print("No active wheel positions.")
    for s in states:
        print(f"[{s.ticker}] {s.state}: {s.qty} shares/equivalent @ ${s.cost_basis:.2f} {s.note}")
        
    print("\n" + "="*40)
    print("🔎 CSP CANDIDATES (Cash: ${:,.2f})".format(args.cash))
    print("="*40)
    
    q_client = _build_quote_client()
    if not q_client:
        print("Tiger QuoteClient not available. Skipping option chains.")
        return 0
        
    targets = set(args.tickers)
    for s in states:
        targets.add(s.ticker)
    
    csps = scan_csps(q_client, list(targets), args.cash)
    if not csps:
        print("No CSP candidates found.")
    for c in csps[:10]:
        print(f"{c.ticker} {c.expiry} ${c.strike}P | Prem: ${c.premium:.2f} | Yld: {c.annualized_yield:.1%} | EffCost: ${c.effective_cost:.2f}")

    print("\n" + "="*40)
    print("📈 COVERED CALL OPPORTUNITIES")
    print("="*40)
    ccs = scan_covered_calls(q_client, states)
    if not ccs:
        print("No CC candidates found.")
    for c in ccs[:10]:
        print(f"{c.ticker} {c.expiry} ${c.strike}C | Prem: ${c.premium:.2f} | Yld: {c.annualized_yield:.1%} | Call-away: ${c.call_away_price:.2f}")

    print("\n" + "="*40)
    print("🛡️ PMCC OPPORTUNITIES")
    print("="*40)
    pmccs = scan_pmccs(q_client, states)
    if not pmccs:
        print("No PMCC candidates found.")
    for c in pmccs[:10]:
        print(f"{c.ticker} Long ${c.long_strike}C ({c.long_expiry}) / Short ${c.short_strike}C ({c.short_expiry}) | Prem: ${c.premium:.2f} | Diag Risk: ${c.diagonal_risk:.2f}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

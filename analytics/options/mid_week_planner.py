"""Tool #9: Mid-Week / Short-Dated Expiry Planner.

Targets 0-5 DTE options (Monday/Wednesday/Friday expiries).
Checks assignment risk for physically settled contracts.
Outputs templates for Protective Hedge, Directional Trade, and Short-Duration Income.

Run: python -m analytics.mid_week_planner SPY QQQ
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from typing import Sequence

from analytics.screening.screener import _build_quote_client, fetch_option_chain
from analytics.options.option_chain import OptionChainSnapshot, OptionQuote, OptionContract

log = logging.getLogger(__name__)

def get_dte(expiry_str: str) -> int:
    try:
        exp = date.fromisoformat(expiry_str.replace("/", "-"))
        return max(0, (exp - date.today()).days)
    except:
        return 30

def main(argv: Sequence[str] | None = None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Mid-Week Expiry Planner.")
    ap.add_argument("tickers", nargs="*", default=["SPY", "QQQ"], help="Tickers to plan (defaults to SPY, QQQ).")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = _build_quote_client()
    if not client:
        print("Tiger QuoteClient not available.")

    for ticker in args.tickers:
        print(f"\n========================================")
        print(f"📅 MID-WEEK PLANNER: {ticker}")
        print(f"========================================")
        
        try:
            import yfinance as yf
            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            tk = yf.Ticker(ticker)
            exps = tk.options
            if not exps:
                print(f"  [!] No options found for {ticker}")
                continue
                
            short_exps = [e for e in exps if 0 <= get_dte(e) <= 5]
            if not short_exps:
                print(f"  [i] No short-dated (0-5 DTE) expiries for {ticker}.")
                continue
                
            for exp in short_exps:
                dte = get_dte(exp)
                exp_date = date.fromisoformat(exp.replace("/", "-"))
                day_name = exp_date.strftime("%A")
                
                print(f"\n[{exp}] {day_name} Expiry ({dte} DTE)")
                
                # Check settlement
                settlement = "Physical Delivery (ASSIGNMENT RISK!)"
                if ticker in ("SPX", "NDX", "RUT", "VIX"):
                    settlement = "Cash Settled (No assignment risk)"
                    
                print(f"  Settlement: {settlement}")
                print(f"  Templates:")
                print(f"    - [Protective Hedge] Buy OTM Put to protect long delta into {day_name} close.")
                print(f"    - [Directional Trade] Buy Debit Spread if post-news catalyst is expected.")
                print(f"    - [Short-Duration Income] Sell Credit Spread (Ensure position is closed by 3:55 PM to avoid assignment!)")
                
        except Exception as e:
            log.warning(f"Failed to fetch {ticker}: {e}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

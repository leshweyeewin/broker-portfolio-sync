"""Tool #3: IV Logger — daily ATM-IV snapshot.

Records the Implied Volatility (IV) for a watchlist of tickers daily.
This provides the historical context needed for IV Percentile (IVP) and
Crush consistency grades (Steps 1-3) in the IV-Crush earnings screener,
because free data sources do not provide historical IV.

Run: python -m analytics.earnings.iv_logger NVDA CRM CRWD COST
"""

from __future__ import annotations

import json
import logging
import argparse
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "iv_history.json"

def fetch_atm_iv(ticker: str) -> float | None:
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
        fast_info = getattr(tk, "fast_info", None)
        price = getattr(fast_info, "last_price", None)
        if not price:
            return None
            
        exps = tk.options
        if not exps:
            return None
            
        # Get nearest expiry (avoiding 0DTE noise if possible)
        target_exp = exps[0]
        if len(exps) > 1:
            target_exp = exps[1]
            
        chain = tk.option_chain(target_exp)
        calls = chain.calls
        if calls.empty:
            return None
            
        calls["dist"] = abs(calls["strike"] - price)
        atm_call = calls.sort_values("dist").iloc[0]
        
        iv = float(atm_call.get("impliedVolatility", 0))
        if iv <= 0:
            return None
        return iv
    except Exception as exc:
        log.debug("Failed to fetch IV for %s: %s", ticker, exc)
        return None

def log_iv_snapshots(tickers: list[str]):
    history = {}
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            pass
            
    today_str = date.today().isoformat()
    updated = 0
    
    for ticker in tickers:
        ticker = ticker.upper()
        iv = fetch_atm_iv(ticker)
        if iv is not None:
            if ticker not in history:
                history[ticker] = {}
            history[ticker][today_str] = round(iv, 4)
            updated += 1
            
    if updated > 0:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, sort_keys=True)
        log.info("Logged ATM-IV for %d tickers.", updated)
    else:
        log.info("No IV data fetched.")

def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
            
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Daily ATM-IV snapshot logger.")
    ap.add_argument("tickers", nargs="*", default=["NVDA", "CRM", "CRWD", "COST", "TSLA", "AAPL"], 
                    help="Watchlist ticker symbols.")
    args = ap.parse_args(argv)
    
    if not args.tickers:
        print("No tickers provided.")
        return 1
        
    log_iv_snapshots(args.tickers)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

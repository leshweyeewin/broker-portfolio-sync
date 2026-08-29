from __future__ import annotations
import argparse
import logging
from datetime import date, datetime, timezone
from typing import Sequence

from analytics.screening.screener import _build_quote_client, fetch_option_chain
from analytics.options.option_chain import OptionChainSnapshot, OptionQuote, OptionContract
from analytics.options.strategies import (
    build_bull_call_spreads,
    build_bear_put_spreads,
    build_long_calls,
    build_long_puts,
    build_cash_secured_puts,
    build_covered_calls,
    build_pmcc_leaps,
    DirectionalFilters
)

log = logging.getLogger(__name__)

def _get_snapshot(client, ticker: str) -> OptionChainSnapshot | None:
    try:
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        tk = yf.Ticker(ticker)
        fi = getattr(tk, "fast_info", None)
        price = getattr(fi, "last_price", None)
        if not price:
            hist = tk.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if not price: return None
            
        exps = tk.options
        if not exps: return None
        
        target_exps = list(exps[:5])
        today = date.today()
        for exp in exps:
            exp_date = date.fromisoformat(exp.replace("/", "-"))
            if (exp_date - today).days > 365:
                if exp not in target_exps:
                    target_exps.append(exp)
                break
                
        quotes = []
        for exp in target_exps:
            chain = fetch_option_chain(ticker, exp, quote_client=client)
            for c in chain:
                right = str(c.get("right", c.get("option_type", c.get("type", "")))).lower()
                if right not in ("call", "put"): continue
                
                strike = float(c.get("strike", c.get("strike_price", 0)))
                bid = float(c.get("bid_price", c.get("bid", 0)))
                ask = float(c.get("ask_price", c.get("ask", 0)))
                oi = int(c.get("open_interest", 0))
                delta = c.get("delta")
                if delta is not None: delta = float(delta)
                
                contract = OptionContract(ticker, ticker, date.fromisoformat(exp.replace("/", "-")), strike, right)
                # OptionQuote signature: OptionQuote(contract, as_of, source, bid, ask, last, volume, open_interest, implied_volatility, delta, theta, gamma, vega)
                quotes.append(OptionQuote(contract, datetime.now(timezone.utc), "broker_live" if client else "delayed", bid=bid, ask=ask, last=None, volume=0, open_interest=oi, implied_volatility=None, delta=delta))
                
        return OptionChainSnapshot(
            snapshot_id=f"dir_bld_{ticker}_{date.today().isoformat()}",
            underlying=ticker,
            underlying_price=price,
            quotes=tuple(quotes),
            as_of=datetime.now(timezone.utc),
            source="broker_live" if client else "delayed"
        )
    except Exception as e:
        log.warning(f"Failed to build snapshot for {ticker}: {e}")
        return None

def main(argv: Sequence[str] | None = None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except: pass

    ap = argparse.ArgumentParser(description="Directional Strategy Builder.")
    ap.add_argument("tickers", nargs="*", help="Watchlist ticker symbols.")
    args = ap.parse_args(argv)

    if not args.tickers:
        print("No tickers provided.")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = _build_quote_client()
    if not client:
        print("Tiger QuoteClient not available. Option chains might not be fetched optimally.")

    for ticker in args.tickers:
        print(f"\nScanning {ticker}...")
        snap = _get_snapshot(client, ticker)
        if not snap:
            print(f"  [!] Could not fetch data for {ticker}")
            continue
            
        print(f"  Spot Price: ")
        expiries = sorted({q.contract.expiry for q in snap.quotes})
        for exp in expiries:
            bcs = build_bull_call_spreads(snap, exp)
            if bcs.candidates:
                c = bcs.candidates[0]
                strikes = "/".join([str(l.strike) for l in c.legs])
                print(f"  [Bull Call] {exp} {strikes} | Debit:  | Max Loss:  | Max Profit:  | RoR: {c.return_on_risk:.2f}")
                
            bps = build_bear_put_spreads(snap, exp)
            if bps.candidates:
                c = bps.candidates[0]
                strikes = "/".join([str(l.strike) for l in c.legs])
                print(f"  [Bear Put]  {exp} {strikes} | Debit:  | Max Loss:  | Max Profit:  | RoR: {c.return_on_risk:.2f}")
                
            lc = build_long_calls(snap, exp)
            if lc.candidates:
                c = lc.candidates[0]
                strikes = str(c.legs[0].strike)
                print(f"  [Long Call] {exp} {strikes}C | Debit:  | Max Loss:  | Breakeven: ")
                
            lp = build_long_puts(snap, exp)
            if lp.candidates:
                c = lp.candidates[0]
                strikes = str(c.legs[0].strike)
                print(f"  [Long Put]  {exp} {strikes}P | Debit:  | Max Loss:  | Breakeven: ")
                
            csp = build_cash_secured_puts(snap, exp)
            if csp.candidates:
                c = csp.candidates[0]
                strikes = str(c.legs[0].strike)
                print(f"  [CSP]       {exp} {strikes}P | Credit: ${c.net_debit * -1:.2f} | Capital Required: ${c.legs[0].strike * 100:.2f}")

            cc = build_covered_calls(snap, exp)
            if cc.candidates:
                c = cc.candidates[0]
                strikes = str(c.legs[0].strike)
                print(f"  [Cov Call]  {exp} {strikes}C | Credit: ${c.net_debit * -1:.2f}")
                
            leaps = build_pmcc_leaps(snap, exp)
            if leaps.candidates:
                c = leaps.candidates[0]
                strikes = str(c.legs[0].strike)
                print(f"  [PMCC Buy]  {exp} {strikes}C | Debit: ${c.net_debit:.2f} | Delta: >0.70")
                
        leaps_expiries = [e for e in expiries if (e - date.today()).days > 365]
        near_term_expiries = [e for e in expiries if 21 <= (e - date.today()).days <= 45]
        
        if leaps_expiries and near_term_expiries:
            from analytics.options.strategies import build_full_pmcc
            pmcc = build_full_pmcc(snap, leaps_expiries[0], near_term_expiries[0])
            if pmcc.candidates:
                c = pmcc.candidates[0]
                print(f"  [PMCC Full] Buy {c.leaps_expiry} {c.leaps_strike}C / Sell {c.short_expiry} {c.short_strike}C | Debit: ${c.net_debit:.2f} | Approx Max Profit: ${c.approx_max_profit:.2f}")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())

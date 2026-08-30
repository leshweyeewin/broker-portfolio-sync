"""Tool #4: Earnings Planner — auto-fills the Earnings Plan sheet.

Runs the IV-Crush screener for a watchlist, formats the results, and 
writes them to the 'Earnings Plan' tab in the Google Sheet.

Run: python -m analytics.earnings.earnings_planner NVDA CRM CRWD COST
"""

from __future__ import annotations

import logging
import argparse
from typing import Any

from analytics.earnings.iv_crush import scan_iv_crush, IVCrushCandidate
from config.settings import get_service_account_info, get_spreadsheet_id
from sheets.writer import PortfolioWriter, SheetClient, EARNINGS_PLAN_HEADERS

log = logging.getLogger(__name__)

def build_earnings_plan_row(cand: IVCrushCandidate) -> list[Any]:
    imp = round(cand.implied_move_pct, 1) if cand.implied_move_pct is not None else ""
    hist = round(cand.hist_move_pct, 1) if cand.hist_move_pct is not None else ""
    ivp = round(cand.iv_percentile, 1) if cand.iv_percentile is not None else ""
    return [
        cand.ticker,
        cand.earnings_date.isoformat(),
        cand.days_left,
        cand.signal,
        imp,
        hist,
        cand.bias,
        cand.strategy,
        cand.em_lower if cand.em_lower is not None else "",
        cand.em_upper if cand.em_upper is not None else "",
        ivp,
        cand.grade,
        cand.verdict,
        cand.verdict_reason,
    ]

def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
            
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Earnings Planner Sheet Auto-filler.")
    ap.add_argument("tickers", nargs="*", help="Watchlist ticker symbols.")
    ap.add_argument("--from-brokers", action="store_true",
                    help="Derive the watchlist from live MooMoo + Tiger holdings.")
    args = ap.parse_args(argv)

    tickers = [t.upper() for t in args.tickers]
    if args.from_brokers:
        from analytics.earnings.watchlist import live_watchlist
        broker_tickers = live_watchlist()
        log.info("Broker-derived watchlist (%d): %s", len(broker_tickers),
                 ", ".join(broker_tickers) or "(none)")
        tickers = sorted(set(tickers) | set(broker_tickers))
    if not tickers:
        tickers = ["NVDA", "CRM", "CRWD", "COST", "TSLA", "AAPL"]  # bare/unreachable fallback

    cands = scan_iv_crush(tickers)
    rows = [build_earnings_plan_row(c) for c in cands]
    
    if not rows:
        log.info("No candidates generated for the planner.")
        return 0
        
    try:
        client = SheetClient(get_service_account_info(), get_spreadsheet_id())
        writer = PortfolioWriter(client)
        writer.ensure_tabs()  # create the "Earnings Plan" tab if the sheet lacks it
        # Header row first — writer._ensure_headers doesn't cover this tab, so the
        # planner owns writing the column labels above its data rows.
        writer.overwrite_earnings_plan([EARNINGS_PLAN_HEADERS] + rows)
        log.info("Successfully wrote %d candidates to Earnings Plan tab.", len(rows))
    except Exception as exc:
        log.error("Failed to write to Google Sheets: %s", exc)
        return 1
        
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

"""Reconcile fixup helper (clears stale/expired open positions from the Google Sheet).

Reads all transaction rows from the Google Sheet (Stocks and Options tabs),
computes current open positions from trades where Status == Open, matches
them against live broker positions, and identifies orphaned/expired positions.

Usage:
    # Dry-run (default: reports mismatches without modifying the sheet)
    python tools/reconcile_fixup.py

    # Apply changes (flips status of expired/stale rows from 'Open' to 'Closed')
    python tools/reconcile_fixup.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.base import AssetType, Broker, BrokerAdapter, OptionTrade, StockTrade
from config.settings import get_service_account_info, get_spreadsheet_id
from core.fifo_pl import compute_option_pl, compute_stock_pl
from core.reconcile import _instrument_key, _norm_strike
from run import _build_adapters
from sheets.writer import (
    PortfolioWriter,
    SheetClient,
    TAB_OPTIONS,
    TAB_STOCKS,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("reconcile_fixup")


def find_stale_open_rows(
    writer: PortfolioWriter,
    adapters: Sequence[BrokerAdapter],
    today: Optional[date] = None,
) -> tuple[list[dict], list[dict]]:
    """Compare Sheet 'Open' rows against live broker positions.

    Returns:
        (stock_fixups, option_fixups)
        where each item is a dict with details about the row to close.
    """
    today = today or date.today()

    # 1. Fetch live broker positions
    broker_positions: dict[tuple[str, str], Decimal] = {}
    for a in adapters:
        try:
            positions = a.fetch_positions()
            for p in positions:
                otype = None if p.asset_type == AssetType.STOCK else p.option_type
                key = _instrument_key(p.symbol, otype, p.strike, p.expiry)
                b_key = (p.broker.value, key)
                broker_positions[b_key] = broker_positions.get(b_key, Decimal("0")) + p.qty
        except Exception as e:
            log.warning("Could not fetch positions from %s: %s", a.name, e)

    # 2. Read Stock trades from Sheet
    stock_items = writer.read_all_stock_trades()
    stock_fixups: list[dict] = []
    for item in stock_items:
        if item["status"] != "Open":
            continue
        trade: StockTrade = item["trade"]
        b_key = (trade.broker.value, _instrument_key(trade.ticker, None, None, None))
        brok_qty = broker_positions.get(b_key, Decimal("0"))
        
        # If broker holds 0 for this stock, it should no longer be open
        if brok_qty == Decimal("0"):
            stock_fixups.append({
                "row_idx": item["row_idx"],
                "broker": trade.broker.value,
                "instrument": trade.ticker,
                "action": trade.action.value,
                "qty": trade.qty,
                "date": trade.date.isoformat(),
                "reason": "Broker holds 0 qty (position closed)",
            })

    # 3. Read Option trades from Sheet
    option_items = writer.read_all_option_trades()
    option_fixups: list[dict] = []
    for item in option_items:
        if item["status"] != "Open":
            continue
        trade: OptionTrade = item["trade"]
        opt_key = _instrument_key(trade.underlying, trade.option_type, trade.strike, trade.expiry)
        b_key = (trade.broker.value, opt_key)
        brok_qty = broker_positions.get(b_key, Decimal("0"))

        is_expired = trade.expiry <= today
        if brok_qty == Decimal("0"):
            reason = "Expired (contract expired)" if is_expired else "Broker holds 0 qty (closed/assigned)"
            option_fixups.append({
                "row_idx": item["row_idx"],
                "broker": trade.broker.value,
                "instrument": f"{trade.underlying} {trade.option_type.value} {trade.strike} ({trade.expiry})",
                "action": trade.action.value,
                "qty": trade.qty,
                "date": trade.date.isoformat(),
                "expiry": trade.expiry.isoformat(),
                "is_expired": is_expired,
                "reason": reason,
            })

    return stock_fixups, option_fixups


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fix stale/expired open positions on Google Sheets.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to Google Sheets (default: dry-run only).",
    )
    args = parser.parse_args(argv)

    adapters = _build_adapters()
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)

    log.info("Checking Google Sheet for stale open positions...")
    stock_fixups, option_fixups = find_stale_open_rows(writer, adapters)

    if not stock_fixups and not option_fixups:
        print("\n[OK] No stale open positions found. Sheet matches broker state cleanly!")
        return 0

    print(f"\nFound {len(stock_fixups)} stale stock rows and {len(option_fixups)} stale option rows:\n")

    if stock_fixups:
        print("--- STOCKS TAB ---")
        for f in stock_fixups:
            print(f"  Row {f['row_idx']:3d} | [{f['broker']}] {f['instrument']:10s} | {f['action']} {f['qty']} | {f['reason']}")
        print()

    if option_fixups:
        print("--- OPTIONS TAB ---")
        for f in option_fixups:
            print(f"  Row {f['row_idx']:3d} | [{f['broker']}] {f['instrument']:35s} | {f['action']} {f['qty']} | {f['reason']}")
        print()

    if args.apply:
        print("[APPLY] Writing updates to Google Sheets...")
        if stock_fixups:
            s_updates = [(f["row_idx"], "Closed") for f in stock_fixups]
            writer.update_status_batch(TAB_STOCKS, s_updates)
            print(f"  Updated {len(s_updates)} stock rows to 'Closed'.")

        if option_fixups:
            o_updates = [(f["row_idx"], "Closed") for f in option_fixups]
            writer.update_status_batch(TAB_OPTIONS, o_updates)
            print(f"  Updated {len(o_updates)} option rows to 'Closed'.")

        print("\n[DONE] Successfully reconciled and closed stale positions.")
    else:
        print("[DRY-RUN] No changes were written to the sheet. Run with --apply to apply these fixes.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

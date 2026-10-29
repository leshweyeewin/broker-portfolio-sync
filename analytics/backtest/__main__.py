"""``python -m analytics.backtest AAPL --rule mean_reversion`` (or a CSV path)"""

from __future__ import annotations

import argparse
from typing import Sequence

from datetime import date, timedelta
from pathlib import Path

from analytics.backtest.bars import load_csv
from analytics.backtest.option_engine import SpreadStructure, run_option_backtest
from analytics.backtest.snapshots import DEFAULT_ROOT, SnapshotLibrary
from analytics.backtest.data import fetch_bars, save_csv
from analytics.backtest.engine import run_backtest
from analytics.backtest.metrics import compute_metrics, format_report
from analytics.backtest.rules import DEFAULT_RULES


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stock-level rule backtest (read-only).")
    ap.add_argument("source", help="a CSV (date,open,close) or a ticker such as AAPL to fetch live history")
    ap.add_argument("--years", type=float, default=3.0, help="history length when fetching a ticker")
    ap.add_argument("--provider", choices=["longbridge", "yfinance"], help="force one price provider")
    ap.add_argument("--save-csv", help="write fetched bars to this CSV")
    ap.add_argument("--rule", choices=sorted(DEFAULT_RULES), default="mean_reversion")
    ap.add_argument("--period", type=int, default=20)
    ap.add_argument("--k", type=float, default=2.0)
    ap.add_argument("--capital", default="10000")
    ap.add_argument("--structure", choices=["put_credit", "call_credit", "bull_call_debit", "bear_put_debit"],
                    help="trade a defined-risk spread instead of stock")
    ap.add_argument("--pricing", choices=["modelled", "snapshot"], default="modelled",
                    help="spread pricing: Black-Scholes model, or stored chain snapshots")
    ap.add_argument("--snapshots", default=str(DEFAULT_ROOT), help="snapshot folder (snapshot pricing)")
    ap.add_argument("--width", type=float, default=1.0)
    ap.add_argument("--delta", type=float, help="target |delta| (default 0.20 credit / 0.50 debit)")
    ap.add_argument("--dte", type=int, default=35)
    ap.add_argument("--risk-pct", type=float, default=0.02, help="max loss per trade as a share of equity")
    ap.add_argument("--fill", choices=["natural", "mid"], default="natural", help="snapshot entry fills")
    ap.add_argument("--stop", type=float, default=1.0, help="credit spreads: stop at x * credit (0 = off)")
    ap.add_argument("--target", type=float, default=0.5, help="credit spreads: take profit at x of credit (0 = off)")
    ap.add_argument("--debit-stop", type=float, default=0.5, help="debit spreads: stop at x * debit (0 = off)")
    ap.add_argument("--debit-target", type=float, default=0.5, help="debit spreads: take profit at x of max gain (0 = off)")
    ap.add_argument("--fee", default=None, help="fee per fill (stock) / per contract leg (options, default 1.00)")
    ap.add_argument("--slippage-bps", default="5")
    args = ap.parse_args(argv)
    rule = DEFAULT_RULES[args.rule](period=args.period, k=args.k)
    if args.source.lower().endswith(".csv"):
        bars = load_csv(args.source)
    else:
        end = date.today()
        providers = (args.provider,) if args.provider else ("longbridge", "yfinance")
        bars, used = fetch_bars(args.source, end - timedelta(days=int(args.years * 365)), end, providers)
        print(f"Fetched {len(bars)} bars for {args.source.upper()} via {used}")
        if args.save_csv:
            save_csv(bars, args.save_csv)
    if args.structure:
        st = SpreadStructure(args.structure, width=args.width, target_delta=args.delta, dte=args.dte)
        ticker = Path(args.source).stem.upper() if args.source.lower().endswith(".csv") else args.source.upper()
        result = run_option_backtest(
            bars, rule, st, ticker=ticker, pricing=args.pricing, capital=args.capital, risk_pct=args.risk_pct,
            fee_per_contract=float(args.fee) if args.fee is not None else 1.0, fill_mode=args.fill,
            stop_multiple=args.stop or None, profit_target=args.target or None,
            debit_stop=args.debit_stop or None, debit_target=args.debit_target or None,
            snapshots=SnapshotLibrary(Path(args.snapshots)) if args.pricing == "snapshot" else None)
        print(format_report(result, compute_metrics(result)))
        return 0
    result = run_backtest(bars, rule, capital=args.capital,
                          fee_per_fill=args.fee or "0", slippage_bps=args.slippage_bps)
    print(format_report(result, compute_metrics(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

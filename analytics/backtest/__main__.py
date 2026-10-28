"""``python -m analytics.backtest AAPL --rule mean_reversion`` (or a CSV path)"""

from __future__ import annotations

import argparse
from typing import Sequence

from datetime import date, timedelta

from analytics.backtest.bars import load_csv
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
    ap.add_argument("--fee", default="0", help="fixed fee per fill")
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
    result = run_backtest(bars, rule, capital=args.capital,
                          fee_per_fill=args.fee, slippage_bps=args.slippage_bps)
    print(format_report(result, compute_metrics(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m analytics.backtest prices.csv --rule mean_reversion``"""

from __future__ import annotations

import argparse
from typing import Sequence

from analytics.backtest.bars import load_csv
from analytics.backtest.engine import run_backtest
from analytics.backtest.metrics import compute_metrics, format_report
from analytics.backtest.rules import DEFAULT_RULES


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stock-level rule backtest (read-only).")
    ap.add_argument("csv", help="daily bars: date,open,close")
    ap.add_argument("--rule", choices=sorted(DEFAULT_RULES), default="mean_reversion")
    ap.add_argument("--period", type=int, default=20)
    ap.add_argument("--k", type=float, default=2.0)
    ap.add_argument("--capital", default="10000")
    ap.add_argument("--fee", default="0", help="fixed fee per fill")
    ap.add_argument("--slippage-bps", default="5")
    args = ap.parse_args(argv)
    rule = DEFAULT_RULES[args.rule](period=args.period, k=args.k)
    result = run_backtest(load_csv(args.csv), rule, capital=args.capital,
                          fee_per_fill=args.fee, slippage_bps=args.slippage_bps)
    print(format_report(result, compute_metrics(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

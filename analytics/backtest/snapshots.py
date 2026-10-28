"""Stage 3 data: record dated option-chain snapshots and read them back.

Layout: ``<root>/<TICKER>/<YYYY-MM-DD>.json.gz`` (one chain per ticker per day,
trimmed to the DTE/strike band the backtests use).  Read-only against brokers.

    python -m analytics.backtest.snapshots record AAPL AMZN
    python -m analytics.backtest.snapshots record --from-brokers
    python -m analytics.backtest.snapshots list
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from analytics.options.option_chain import (
    OptionChainSnapshot, OptionContract, OptionQuote, _snapshot_from_dict, _snapshot_to_dict,
)

log = logging.getLogger(__name__)
DEFAULT_ROOT = Path("analytics") / "data" / "chain_history"


def save_snapshot(snapshot: OptionChainSnapshot, root: Path = DEFAULT_ROOT) -> Path:
    path = root / snapshot.underlying / f"{snapshot.as_of.date().isoformat()}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(_snapshot_to_dict(snapshot), fh, sort_keys=True)
    tmp.replace(path)
    return path


class SnapshotLibrary:
    def __init__(self, root: Path = DEFAULT_ROOT) -> None:
        self.root = Path(root)

    def days(self, ticker: str) -> list[date]:
        d = self.root / ticker.upper()
        return sorted(date.fromisoformat(p.name[:10]) for p in d.glob("*.json.gz")) if d.exists() else []

    def get(self, ticker: str, day: date) -> OptionChainSnapshot | None:
        path = self.root / ticker.upper() / f"{day.isoformat()}.json.gz"
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return _snapshot_from_dict(json.load(fh))


def _f(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def build_snapshot(ticker: str, *, client=None, dte_min: int = 7, dte_max: int = 50,
                   strike_band: float = 0.20, now: datetime | None = None) -> OptionChainSnapshot | None:
    """Fetch a chain (Tiger if available, else yfinance) keeping implied vol."""
    import yfinance as yf

    from analytics.screening.screener import fetch_option_chain

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    now = now or datetime.now(timezone.utc)
    tk = yf.Ticker(ticker)
    spot = _f(getattr(getattr(tk, "fast_info", None), "last_price", None))
    if not spot:
        hist = tk.history(period="1d")
        spot = float(hist["Close"].iloc[-1]) if not hist.empty else None
    if not spot:
        return None
    today = now.date()
    quotes = []
    for exp in tk.options or ():
        exp_date = date.fromisoformat(exp.replace("/", "-"))
        if not (dte_min <= (exp_date - today).days <= dte_max):
            continue
        for c in fetch_option_chain(ticker, exp, quote_client=client):
            right = str(c.get("right", c.get("option_type", ""))).lower()
            strike = _f(c.get("strike", c.get("strike_price")))
            if right not in ("call", "put") or not strike or abs(strike / spot - 1) > strike_band:
                continue
            iv = _f(c.get("implied_volatility", c.get("iv")))
            quotes.append(OptionQuote(
                OptionContract(str(c.get("identifier") or f"{ticker}{exp}{right[0]}{strike}"), ticker, exp_date, strike, right),
                now, "broker_live" if client else "delayed",
                bid=_f(c.get("bid_price", c.get("bid"))), ask=_f(c.get("ask_price", c.get("ask"))),
                volume=int(_f(c.get("volume")) or 0), open_interest=int(_f(c.get("open_interest")) or 0),
                implied_volatility=iv if iv and iv > 0.001 else None, delta=_f(c.get("delta"))))
    if not quotes:
        return None
    return OptionChainSnapshot(ticker, spot, now, "broker_live" if client else "delayed", tuple(quotes))


def record(tickers: Sequence[str], root: Path = DEFAULT_ROOT) -> int:
    try:
        from analytics.screening.screener import _build_quote_client
        client = _build_quote_client()
    except Exception:  # noqa: BLE001
        client = None
    saved = 0
    for t in tickers:
        try:
            snap = build_snapshot(t.upper(), client=client)
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not stop the run
            log.warning("snapshot failed for %s: %s", t, exc)
            continue
        if snap:
            save_snapshot(snap, root)
            saved += 1
    log.info("Recorded %d/%d chain snapshots into %s", saved, len(tickers), root)
    return saved


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Record/list option-chain snapshots for stage-3 backtests.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("tickers", nargs="*")
    rec.add_argument("--from-brokers", action="store_true", help="add the live MooMoo + Tiger watchlist")
    rec.add_argument("--root", default=str(DEFAULT_ROOT))
    ls = sub.add_parser("list")
    ls.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args(argv)
    if args.cmd == "list":
        lib = SnapshotLibrary(Path(args.root))
        for d in sorted(p.name for p in lib.root.glob("*") if p.is_dir()):
            days = lib.days(d)
            print(f"{d}: {len(days)} days ({days[0]} -> {days[-1]})" if days else f"{d}: none")
        return 0
    tickers = [t.upper() for t in args.tickers]
    if args.from_brokers:
        from analytics.earnings.watchlist import live_watchlist
        tickers = sorted(set(tickers) | set(live_watchlist()))
    if not tickers:
        ap.error("give tickers or --from-brokers")
    record(tickers, Path(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

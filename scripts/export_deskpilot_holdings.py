#!/usr/bin/env python3
"""Export current holdings (Status == "Open") to Deskpilot's Holdings CSV format.

broker-portfolio-sync keeps a full *trade ledger* (every execution) in the Stocks
and Options tabs, with a ``Status`` column marking whether each lot is still Open.
Deskpilot, by contrast, wants a *current-holdings snapshot*. This script bridges
the two: it reads the Stocks and Options rows, keeps only ``Status == "Open"``,
aggregates them into net positions, and writes a CSV in the exact schema
``deskpilot.tools.portfolio.load_portfolio`` expects:

    kind,broker,ticker,type,qty,avg_cost,market_price,strike,expiry,action,premium,currency,amount

Usage
-----
Read straight from the published sheet (default) and print the Holdings CSV::

    python scripts/export_deskpilot_holdings.py \
        --cash "SGD=12450,USD=3120" --out holdings.csv

Read local CSV exports of the two tabs instead of the network::

    python scripts/export_deskpilot_holdings.py \
        --stocks-file Stocks.csv --options-file Options.csv --cash "SGD=12450"

Then paste holdings.csv into a new "Holdings" tab, File -> Share -> Publish to web
-> that tab -> CSV, and point Deskpilot's DESKPILOT_PORTFOLIO_CSV_URL at it.

Notes
-----
* **Cash** is not derivable from the trade tabs (free buying power depends on
  settlement, FX, and fees the ledger doesn't fully resolve), so pass it with
  ``--cash``. If broker-portfolio-sync already computes free cash internally, wire
  that in place of the ``--cash`` argument.
* **market_price** is filled best-effort via yfinance when available (US and
  ``.SI`` symbols resolve; others are left blank and Deskpilot's MarketAnalyst
  fetches them live). Disable with ``--no-prices``.
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections import defaultdict
from typing import Any

# Published-sheet CSV links (the broker-portfolio-sync workbook). Override with
# --stocks-file / --options-file to read local exports instead.
_PUB = ("https://docs.google.com/spreadsheets/d/e/"
        "2PACX-1vT9-Jch7iUleSXRvAsQJlIk1L5NxLwE6LzSfmuMIs_zKkv3XfD3Z6GhgKtCOVQEqNLLD5C2Do2mBX76")
DEFAULT_STOCKS_URL = f"{_PUB}/pub?gid=178567232&single=true&output=csv"
DEFAULT_OPTIONS_URL = f"{_PUB}/pub?gid=2056984467&single=true&output=csv"

HOLDINGS_HEADER = [
    "kind", "broker", "ticker", "type", "qty", "avg_cost", "market_price",
    "strike", "expiry", "action", "premium", "currency", "amount",
]


def _money(x: Any) -> float | None:
    """Parse a spreadsheet money/number cell: strips $ , spaces; ( ) = negative."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("$", "").replace(" ", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _read_rows(source: str, is_url: bool) -> list[dict[str, str]]:
    """Read a Stocks/Options CSV into dicts, locating the real header row.

    The published Stocks/Options tabs carry two summary rows above the header, so
    we scan for the row that starts the column headers (contains "Date").
    """
    if is_url:
        with urllib.request.urlopen(source, timeout=30) as resp:  # noqa: S310
            text = resp.read().decode("utf-8-sig")
    else:
        with open(source, "r", encoding="utf-8-sig") as fh:
            text = fh.read()

    all_rows = list(csv.reader(io.StringIO(text)))
    header_idx = next(
        (i for i, r in enumerate(all_rows) if r and r[0].strip() == "Date"), None
    )
    if header_idx is None:
        return []
    header = [h.strip() for h in all_rows[header_idx]]
    out: list[dict[str, str]] = []
    for r in all_rows[header_idx + 1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({header[i]: (r[i] if i < len(r) else "") for i in range(len(header))})
    return out


def build_holdings(
    stock_rows: list[dict[str, Any]],
    option_rows: list[dict[str, Any]],
    cash: dict[str, float] | None = None,
) -> list[list[Any]]:
    """Aggregate Status=Open rows into Deskpilot Holdings rows (pure/testable)."""
    out: list[list[Any]] = []

    # --- Stocks: net qty + weighted-average cost per (broker, ticker, currency) ---
    stock_agg: dict[tuple, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "cost": 0.0})
    for r in stock_rows:
        if (r.get("Status") or "").strip().lower() != "open":
            continue
        qty = _money(r.get("Qty"))
        price = _money(r.get("Price"))
        if qty is None:
            continue
        key = ((r.get("Broker") or "").strip(),
               (r.get("Ticker") or "").strip(),
               (r.get("Currency") or "USD").strip().upper())
        stock_agg[key]["qty"] += qty
        if price is not None:
            stock_agg[key]["cost"] += qty * price

    for (broker, ticker, currency), a in sorted(stock_agg.items()):
        if round(a["qty"], 6) == 0:
            continue
        avg = round(a["cost"] / a["qty"], 4) if a["qty"] else ""
        out.append(["position", broker, ticker, "", _fmt(a["qty"]), avg, "",
                    "", "", "", "", currency, ""])

    # --- Options: net qty + weighted-avg premium per (broker, underlying, type, strike, expiry) ---
    opt_agg: dict[tuple, dict[str, float]] = defaultdict(lambda: {"qty": 0.0, "prem_w": 0.0, "w": 0.0})
    for r in option_rows:
        if (r.get("Status") or "").strip().lower() != "open":
            continue
        qty = _money(r.get("Qty"))
        prem = _money(r.get("Premium"))
        if qty is None:
            continue
        key = ((r.get("Broker") or "").strip(),
               (r.get("Stock") or "").strip(),
               (r.get("Type") or "").strip().upper(),
               (r.get("Strike") or "").strip(),
               (r.get("Expiry") or "").strip(),
               (r.get("Currency") or "USD").strip().upper())
        opt_agg[key]["qty"] += qty
        if prem is not None:
            opt_agg[key]["prem_w"] += abs(qty) * prem
            opt_agg[key]["w"] += abs(qty)

    for (broker, underlying, otype, strike, expiry, currency), a in sorted(opt_agg.items()):
        if round(a["qty"], 6) == 0:
            continue
        premium = round(a["prem_w"] / a["w"], 4) if a["w"] else ""
        action = "sell-to-open" if a["qty"] < 0 else "buy-to-open"
        out.append(["option", broker, underlying, otype, _fmt(a["qty"]), "", "",
                    _money(strike) if strike else "", expiry, action, premium, currency, ""])

    # --- Cash: supplied explicitly (not derivable from the trade tabs) ---
    for currency, amount in (cash or {}).items():
        out.append(["cash", "", "", "", "", "", "", "", "", "", "", currency.upper(), amount])

    return out


def _fmt(n: float) -> Any:
    """Render whole numbers without a trailing .0 (share/contract counts)."""
    return int(n) if float(n).is_integer() else round(n, 4)


def _enrich_prices(rows: list[list[Any]]) -> None:
    """Best-effort: fill market_price for stock positions via yfinance (in place)."""
    try:
        import yfinance as yf
    except ImportError:
        return
    for row in rows:
        if row[0] != "position" or row[6]:  # not a position, or price already set
            continue
        symbol = str(row[2]).strip()
        if not symbol:
            continue
        try:
            hist = yf.Ticker(symbol).history(period="1d")
            if hist is not None and not hist.empty:
                row[6] = round(float(hist["Close"].iloc[-1]), 2)
        except Exception:  # noqa: BLE001 - best-effort only
            continue


def _parse_cash(spec: str | None) -> dict[str, float]:
    cash: dict[str, float] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        cur, _, amt = part.partition("=")
        val = _money(amt)
        if val is not None:
            cash[cur.strip().upper()] = val
    return cash


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stocks-file", help="local Stocks CSV (default: published sheet)")
    ap.add_argument("--options-file", help="local Options CSV (default: published sheet)")
    ap.add_argument("--stocks-url", default=DEFAULT_STOCKS_URL)
    ap.add_argument("--options-url", default=DEFAULT_OPTIONS_URL)
    ap.add_argument("--cash", help='free cash per currency, e.g. "SGD=12450,USD=3120"')
    ap.add_argument("--no-prices", action="store_true", help="skip yfinance market_price lookup")
    ap.add_argument("--out", help="output CSV path (default: stdout)")
    args = ap.parse_args(argv)

    stock_rows = _read_rows(args.stocks_file or args.stocks_url, is_url=not args.stocks_file)
    option_rows = _read_rows(args.options_file or args.options_url, is_url=not args.options_file)

    rows = build_holdings(stock_rows, option_rows, _parse_cash(args.cash))
    if not args.no_prices:
        _enrich_prices(rows)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HOLDINGS_HEADER)
    w.writerows(rows)
    csv_text = buf.getvalue()

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        n_pos = sum(1 for r in rows if r[0] == "position")
        n_opt = sum(1 for r in rows if r[0] == "option")
        print(f"wrote {args.out}: {n_pos} positions, {n_opt} options, "
              f"{sum(1 for r in rows if r[0] == 'cash')} cash rows", file=sys.stderr)
    else:
        sys.stdout.write(csv_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

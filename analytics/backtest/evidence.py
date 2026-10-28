"""Cached per-ticker rule evidence for alerts (Telegram quote, weekly digest).

A weekly refresh runs the default stock rules over ~3y of daily bars per ticker
and stores compact metrics in ``analytics/data/rule_evidence.json``.  Alerts
only *read* the cache (``evidence_line``), so they stay fast and offline.  The
evidence is context for a setup, not a signal: small samples are labelled.

    python -m analytics.backtest.evidence NVDA AMZN
    python -m analytics.backtest.evidence --from-brokers
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

from analytics.backtest.data import fetch_bars
from analytics.backtest.engine import run_backtest
from analytics.backtest.metrics import MIN_TRADES, compute_metrics
from analytics.backtest.rules import mean_reversion, trend_following

log = logging.getLogger(__name__)
CACHE = Path("analytics") / "data" / "rule_evidence.json"
STALE_DAYS = 6
_RULES = {"mean-reversion": mean_reversion, "trend": trend_following}


def evidence_for(ticker: str, *, years: float = 3.0, today: Optional[date] = None) -> dict:
    today = today or date.today()
    bars, provider = fetch_bars(ticker, today - timedelta(days=int(years * 365)), today)
    out = {"asof": today.isoformat(), "bars": len(bars), "first": bars[0].day.isoformat(),
           "last": bars[-1].day.isoformat(), "provider": provider, "rules": {}}
    for name, factory in _RULES.items():
        m = compute_metrics(run_backtest(bars, factory()))
        out["rules"][name] = {
            "trades": m.trades, "win_rate": m.win_rate, "erases": m.wins_erased_by_one_loss,
            "return_pct": round(m.total_return_pct, 1), "mdd_pct": round(m.max_drawdown_pct, 1),
            "avg_bars": m.avg_bars_held,
        }
    return out


def load_cache(path: Path = CACHE) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def refresh(tickers: Sequence[str], *, path: Path = CACHE, force: bool = False,
            today: Optional[date] = None, fetch=evidence_for) -> int:
    """Refresh stale/missing tickers; one failure never blocks the rest."""
    today = today or date.today()
    cache = load_cache(path)
    done = 0
    for t in (x.upper() for x in tickers):
        old = cache.get(t)
        if old and not force and (today - date.fromisoformat(old["asof"])).days < STALE_DAYS:
            continue
        try:
            cache[t] = fetch(t, today=today)
            done += 1
        except Exception as exc:  # noqa: BLE001 - provider/network errors are per-ticker
            log.warning("rule evidence failed for %s: %s", t, exc)
    if done:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    return done


def format_evidence(ticker: str, ev: dict) -> str:
    parts = []
    for name, r in ev["rules"].items():
        if not r["trades"]:
            parts.append(f"{name}: no trades")
            continue
        tail = f", loss erases {r['erases']:.1f} wins" if r["erases"] else ""
        parts.append(f"{name}: {r['trades']} trades, {r['win_rate'] * 100:.0f}% win{tail}, "
                     f"{r['return_pct']:+.0f}% / dd -{r['mdd_pct']:.0f}%")
    thin = all(r["trades"] < MIN_TRADES for r in ev["rules"].values())
    note = f" ⚠️ <{MIN_TRADES} trades: context only" if thin else ""
    return f"Rule evidence ({ev['first'][:4]}–{ev['last'][:4]} daily, Bollinger 20/2, long stock): " + " | ".join(parts) + note


def evidence_line(ticker: str, *, path: Path = CACHE) -> str:
    ev = load_cache(path).get(ticker.upper())
    return format_evidence(ticker, ev) if ev else ""


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Refresh cached rule evidence.")
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--from-brokers", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    tickers = [t.upper() for t in a.tickers]
    if a.from_brokers:
        from analytics.earnings.watchlist import live_watchlist
        tickers = sorted(set(tickers) | set(live_watchlist()))
    if not tickers:
        ap.error("give tickers or --from-brokers")
    n = refresh(tickers, force=a.force)
    for t in tickers:
        line = evidence_line(t)
        if line:
            print(f"{t}: {line}")
    log.info("refreshed %d/%d", n, len(tickers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

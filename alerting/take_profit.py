"""Live take-profit alert on open long options.

The daily sync and the weekly digests track *realized* P/L and looming expiries,
but nothing watches the *mark-to-market* swing on still-open positions. This does:
it reads live positions straight from the broker adapters (which report
``market_price`` alongside ``avg_cost``) and messages any long option whose
unrealized gain has reached the take-profit threshold — the user's rule: **long
call/put, take profit at +50%**.

Scope is deliberately narrow (single-purpose, like the other alerting jobs):
  * **Long options only** (qty > 0, option asset). A long option's risk is the
    premium paid, so this alert is about capturing the *win*, not stopping a loss
    — drawdown/expiry management already lives in ``analytics/risk_engine.py``.
  * Read-only + best-effort delivery via ``alerting.notify.notify_safe``.

Run:  python -m alerting.take_profit
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from adapters.base import AssetType, Position
from alerting.notify import notify_safe

log = logging.getLogger(__name__)

DEFAULT_TP_PCT = Decimal("50")  # long-option take-profit threshold (%)


@dataclass
class TakeProfitSignal:
    symbol: str
    option_type: str        # "Call" / "Put" (or "" if the adapter didn't set it)
    qty: Decimal
    avg_cost: Decimal
    market_price: Decimal
    pl_pct: Decimal

    def line(self) -> str:
        ot = f" {self.option_type}" if self.option_type else ""
        return (f"🎯 TAKE-PROFIT — {self.symbol}{ot}\n"
                f"   +{self.pl_pct:.1f}% unrealized (cost ${self.avg_cost:.2f} → "
                f"${self.market_price:.2f}). At/above your +{DEFAULT_TP_PCT:g}% target — "
                f"consider closing.")


def unrealized_pct(pos: Position) -> Optional[Decimal]:
    """Unrealized P/L % for a long position, or None if not computable."""
    if pos.market_price is None or pos.avg_cost is None or pos.avg_cost <= 0:
        return None
    return (pos.market_price / pos.avg_cost - 1) * 100


def evaluate_take_profits(
    positions: Sequence[Position],
    tp_pct: Decimal = DEFAULT_TP_PCT,
) -> list[TakeProfitSignal]:
    """Long options whose unrealized gain has reached ``tp_pct``.

    Stocks, short options (qty <= 0), and positions without a live mark are
    skipped — this alert only fires the long-option take-profit rule.
    """
    signals: list[TakeProfitSignal] = []
    for pos in positions:
        if pos.asset_type is not AssetType.OPTION:
            continue
        if pos.qty <= 0:  # long only
            continue
        pct = unrealized_pct(pos)
        if pct is None or pct < tp_pct:
            continue
        signals.append(TakeProfitSignal(
            symbol=pos.symbol,
            option_type=(pos.option_type.value if pos.option_type else ""),
            qty=pos.qty,
            avg_cost=pos.avg_cost,
            market_price=pos.market_price,
            pl_pct=pct,
        ))
    signals.sort(key=lambda s: s.pl_pct, reverse=True)
    return signals


def format_message(signals: Sequence[TakeProfitSignal]) -> str:
    header = f"🎯 Long-Option Take-Profit — {len(signals)} at/above +{DEFAULT_TP_PCT:g}%:"
    return "\n".join([header, ""] + [s.line() for s in signals])


def _gather_live_positions() -> list[Position]:
    """Fetch current positions from every enabled broker adapter, fail-soft."""
    from run import _build_adapters  # lazy: keeps this module import-light + testable

    positions: list[Position] = []
    for adapter in _build_adapters():
        try:
            positions.extend(adapter.fetch_positions())
        except Exception:
            log.warning("fetch_positions failed for %s", getattr(adapter, "name", "?"),
                        exc_info=True)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    return positions


def main(argv=None) -> int:
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Live long-option take-profit alert (+50%).")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the alert instead of sending to Telegram")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    positions = _gather_live_positions()
    signals = evaluate_take_profits(positions)
    if not signals:
        log.info("No long options at/above +%s%% take-profit (%d positions checked).",
                 DEFAULT_TP_PCT, len(positions))
        return 0
    if args.dry_run:
        print(format_message(signals))
        return 0
    ok = notify_safe(format_message(signals))
    if not ok:
        log.error("Take-profit alert send failed (check Telegram config).")
        return 1
    log.info("Sent take-profit alert for %d position(s).", len(signals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

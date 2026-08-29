"""Live post-earnings IV-crush exit reminder.

The moomoo earnings playbook's hardest discipline rule: **the edge is the IV
crush, not holding to expiry — exit the morning after earnings, close at 50–80%
of max profit, never overstay.** Losses in their forward test came from holding
too long into a gap.

This watches open *option* positions and, for any whose underlying reported
earnings in the last couple of days, fires a reminder to review the exit — the
IV has now crushed, so the premium edge on a short credit spread is already
realised. Sibling to ``alerting/take_profit.py``:

  * ``take_profit`` — long options at +50% (capture the win on a directional buy).
  * ``earnings_iv_exit`` — options on a stock that just reported (close the
    volatility trade before theta/gap works against you).

Read-only + best-effort delivery. It never closes anything — it reminds you to.

Run:  python -m alerting.earnings_iv_exit
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from adapters.base import AssetType, Position
from alerting.notify import notify_safe
from analytics.earnings.earnings import get_earnings_dates

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 2  # flag options whose underlying reported within this window


@dataclass
class EarningsExitSignal:
    symbol: str
    option_type: str            # "Call" / "Put" (or "" if unknown)
    qty: Decimal                # signed: negative = short
    strike: Optional[Decimal]
    expiry: Optional[date]
    earnings_date: date
    days_since: int

    @property
    def side(self) -> str:
        return "short" if self.qty < 0 else "long"

    def line(self) -> str:
        ot = f" {self.option_type}" if self.option_type else ""
        k = f" {self.strike:g}" if self.strike is not None else ""
        exp = f" {self.expiry}" if self.expiry else ""
        ago = "today" if self.days_since == 0 else (
            "yesterday" if self.days_since == 1 else f"{self.days_since}d ago")
        return (f"⏰ IV-CRUSH EXIT — {self.symbol}{ot}{k}{exp} "
                f"({self.side} {abs(self.qty):g}) · earnings {self.earnings_date} ({ago})\n"
                f"   IV has crushed. Playbook: close at 50–80% of max profit — "
                f"don't overstay to expiry.")


def recent_earnings(
    symbol: str, today: date, lookback_days: int = DEFAULT_LOOKBACK_DAYS
) -> Optional[date]:
    """Most recent earnings date in ``[today - lookback_days, today]``, else None."""
    best: Optional[date] = None
    for ed in get_earnings_dates(symbol):
        delta = (today - ed).days
        if 0 <= delta <= lookback_days:
            if best is None or ed > best:
                best = ed
    return best


def evaluate_earnings_exits(
    positions: Sequence[Position],
    *,
    today: Optional[date] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[EarningsExitSignal]:
    """Open options whose underlying reported earnings within the lookback window.

    Both long and short options qualify — the IV-crush exit applies to credit
    spreads (short) and to long earnings plays alike. Stocks and closed positions
    (qty == 0) are skipped.
    """
    today = today or date.today()
    signals: list[EarningsExitSignal] = []
    for pos in positions:
        if pos.asset_type is not AssetType.OPTION or pos.qty == 0:
            continue
        ed = recent_earnings(pos.symbol, today, lookback_days)
        if ed is None:
            continue
        signals.append(EarningsExitSignal(
            symbol=pos.symbol,
            option_type=(pos.option_type.value if pos.option_type else ""),
            qty=pos.qty,
            strike=pos.strike,
            expiry=pos.expiry,
            earnings_date=ed,
            days_since=(today - ed).days,
        ))
    signals.sort(key=lambda s: (s.days_since, s.symbol))
    return signals


def format_message(signals: Sequence[EarningsExitSignal]) -> str:
    header = f"⏰ Post-Earnings IV-Crush Exit — {len(signals)} option(s) to review:"
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
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="Live post-earnings IV-crush exit reminder for open options.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the alert instead of sending to Telegram")
    ap.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help="flag options whose earnings was within N days (default %(default)s)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    positions = _gather_live_positions()
    signals = evaluate_earnings_exits(positions, lookback_days=args.lookback_days)
    if not signals:
        log.info("No open options with earnings in the last %d day(s) (%d positions checked).",
                 args.lookback_days, len(positions))
        return 0
    if args.dry_run:
        print(format_message(signals))
        return 0
    ok = notify_safe(format_message(signals))
    if not ok:
        log.error("Earnings-exit alert send failed (check Telegram config).")
        return 1
    log.info("Sent earnings IV-crush exit alert for %d option(s).", len(signals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

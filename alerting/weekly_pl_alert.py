"""Weekly realized-P/L Telegram digest.

Every Sunday, read the sheet's closed trades, sum the realized P/L (SGD) for the
week just ended (ISO week: Monday..today), broken down by broker, and send a
digest. Read-only; the same realized figure the sync writes to the sheet, so the
digest and the Dashboard's "This Week Realized" row always agree.

Realized only — this is booked P/L on *closed* trades, not the mark-to-market
swing on open positions. Best-effort delivery via ``alerting.notify.notify_safe``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Optional

from alerting.notify import notify_safe
from lemon8.reader import ClosedPosition, read_closed_positions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerWeekPL:
    broker: str
    pl_sgd: Decimal
    trades: int
    wins: int

    @property
    def win_rate(self) -> int:
        return round(self.wins / self.trades * 100) if self.trades else 0


# --------------------------------------------------------------------------- #
# Compute
# --------------------------------------------------------------------------- #

def compute_weekly_pl(
    closed: list[ClosedPosition], *, today: date
) -> list[BrokerWeekPL]:
    """Realized P/L (SGD) per broker for trades closed this ISO week (Mon..today).

    Only rows with a booked ``realized_pl_sgd`` count — so the total matches the
    sheet's Realized figure, not the raw closed-row count. Sorted by P/L desc.
    """
    monday = today - timedelta(days=today.weekday())
    agg: dict[str, list] = {}
    for p in closed:
        if p.realized_pl_sgd is None:
            continue
        d = _parse_iso(p.close_date)
        if d is None or not (monday <= d <= today):
            continue
        a = agg.setdefault(p.broker, [Decimal(0), 0, 0])
        a[0] += p.realized_pl_sgd
        a[1] += 1
        a[2] += 1 if p.realized_pl_sgd > 0 else 0
    out = [BrokerWeekPL(b, pl, n, w) for b, (pl, n, w) in agg.items()]
    out.sort(key=lambda x: x.pl_sgd, reverse=True)
    return out


def _parse_iso(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #

def format_weekly_pl_message(results: list[BrokerWeekPL], *, today: date) -> str:
    monday = today - timedelta(days=today.weekday())
    week = f"{monday:%d %b} – {today:%d %b %Y}"

    if not results:
        return f"\U0001f4c8 Weekly P/L — {week}\n\nNo trades closed this week."

    lines = [f"\U0001f4c8 Weekly P/L (realized) — {week}", ""]
    total_pl = Decimal(0)
    total_n = 0
    for r in results:
        lines.append(
            f"   {r.broker}: {_money(r.pl_sgd)} SGD  "
            f"({r.trades} {'trade' if r.trades == 1 else 'trades'} · {r.win_rate}% win)"
        )
        total_pl += r.pl_sgd
        total_n += r.trades
    lines.append("")
    lines.append(f"Total realized: {_money(total_pl)} SGD  ({total_n} trades)")
    lines.append("")
    lines.append("Realized/closed trades only — open-position moves not included.")
    return "\n".join(lines)


def _money(v: Decimal) -> str:
    return f"{v:+,.2f}"


# --------------------------------------------------------------------------- #
# Orchestration + CLI
# --------------------------------------------------------------------------- #

def run_weekly_pl_alert(
    client,
    *,
    notifier: Callable[[str], bool] = notify_safe,
    today: Optional[date] = None,
) -> tuple[list[BrokerWeekPL], bool]:
    """Compute the week's realized P/L, send the digest, return (results, delivered)."""
    today = today or date.today()
    results = compute_weekly_pl(read_closed_positions(client), today=today)
    delivered = notifier(format_weekly_pl_message(results, today=today))
    return results, delivered


def main(argv=None) -> int:
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from config.settings import get_service_account_info, get_spreadsheet_id
    from sheets.writer import SheetClient

    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    results, delivered = run_weekly_pl_alert(client)

    total = sum((r.pl_sgd for r in results), Decimal(0))
    log.info("Weekly P/L alert: %d broker(s), total %s SGD; delivered=%s",
             len(results), f"{total:+,.2f}", delivered)
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())

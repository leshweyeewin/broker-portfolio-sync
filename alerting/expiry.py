"""Weekly Telegram alert for options expiring within the next 7 days.

The daily 06:00 sync keeps the sheet current but never nudges you about a
looming expiry. This runs on its own cadence (Sunday evening) — it reads the
Options tab, nets each contract's open quantity, and messages every contract
that expires in the coming week so a roll/close isn't missed.

Design
------
* **Read-only** — reuses the sheet the sync already maintains; touches no sync
  logic. Columns are found by *header name* (§9), so a writer column reorder
  fails loud rather than silently mapping the wrong field.
* **Net, not per-row.** A contract can have several journal rows (opening
  balance + buys + sells). We sum signed quantity per contract (Buy / Opening
  Balance add, Sell subtracts) and keep only the ones still open (net ≠ 0). This
  is more robust than trusting the per-row FIFO ``Status``.
* **Best-effort delivery** — sends via ``alerting.notify.notify_safe``; a broken
  bot config surfaces as a non-zero exit from ``main`` rather than a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from sheets.writer import (
    OPTIONS_HEADERS,
    TAB_OPTIONS,
    DATA_HEADER_ROWS,
    _col_letter,
)
from alerting.notify import notify_safe

log = logging.getLogger(__name__)

DEFAULT_WITHIN_DAYS = 7

# Buy and Opening Balance add to a lot (+qty); Sell disposes (−qty). Mirrors
# adapters.base.OptionAction.is_acquisition, read back from the sheet's strings.
_ACQUIRE_ACTIONS = {"Buy", "Opening Balance"}

# Columns the alert depends on; missing any of these means the tab shape changed.
_NEEDED = {"Broker", "Stock", "Type", "Strike", "Qty", "Expiry", "Action"}


class ExpiryReadError(RuntimeError):
    """Raised when the Options tab's columns don't match what we expect (§9)."""


@dataclass(frozen=True)
class ExpiringOption:
    """One still-open option contract expiring inside the window."""

    broker: str
    underlying: str
    option_type: str
    strike: str            # display string as stored, e.g. "$130.00"
    expiry: date
    net_qty: Decimal       # + long, − short

    @property
    def side(self) -> str:
        return "long" if self.net_qty > 0 else "short"

    @property
    def label(self) -> str:
        strike = self.strike.lstrip("$").strip()
        parts = [self.underlying, strike, self.option_type]
        return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Read + filter
# --------------------------------------------------------------------------- #

def find_expiring_options(
    client, *, today: date, within_days: int = DEFAULT_WITHIN_DAYS
) -> list[ExpiringOption]:
    """Open option contracts expiring within ``[today, today + within_days]``.

    ``client`` is a ``sheets.writer.SheetClient`` (or any object exposing
    ``get_values(range_) -> list[list]`` — a fake in tests). Result is sorted by
    expiry, then underlying, then strike.
    """
    col_letter = _col_letter(len(OPTIONS_HEADERS))
    values = client.get_values(f"{TAB_OPTIONS}!A:{col_letter}")

    header_idx = DATA_HEADER_ROWS.get(TAB_OPTIONS, 1) - 1
    if len(values) <= header_idx:
        return []

    idx = _index_map(values[header_idx])
    horizon = today + timedelta(days=within_days)

    net: dict[tuple, Decimal] = {}
    for row in values[header_idx + 1:]:
        expiry = _parse_date(_cell(row, idx["Expiry"]))
        if expiry is None or not (today <= expiry <= horizon):
            continue
        qty = _dec(_cell(row, idx["Qty"]))
        if qty is None:
            continue
        action = str(_cell(row, idx["Action"])).strip()
        signed = qty if action in _ACQUIRE_ACTIONS else -qty
        key = (
            str(_cell(row, idx["Broker"])).strip(),
            str(_cell(row, idx["Stock"])).strip(),
            str(_cell(row, idx["Type"])).strip(),
            str(_cell(row, idx["Strike"])).strip(),
            expiry,
        )
        net[key] = net.get(key, Decimal(0)) + signed

    out = [
        ExpiringOption(
            broker=b, underlying=u, option_type=t, strike=s, expiry=e, net_qty=q
        )
        for (b, u, t, s, e), q in net.items()
        if q != 0
    ]
    out.sort(key=lambda o: (o.expiry, o.underlying, o.strike))
    return out


def _index_map(header_row: list[Any]) -> dict[str, int]:
    present = {str(name).strip(): i for i, name in enumerate(header_row)}
    missing = _NEEDED - present.keys()
    if missing:
        raise ExpiryReadError(
            f"{TAB_OPTIONS} tab is missing expected column(s): {sorted(missing)}. "
            f"Found: {sorted(present.keys())}"
        )
    return present


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #

def format_expiry_message(
    options: list[ExpiringOption], *, today: date, within_days: int = DEFAULT_WITHIN_DAYS
) -> str:
    """Render the Telegram digest — grouped by expiry date, long/short flagged."""
    if not options:
        return (
            f"✅ No open options expiring in the next {within_days} days "
            f"(as of {today:%d %b %Y})."
        )

    lines = [
        f"⏰ Options expiring in the next {within_days} days "
        f"(as of {today:%d %b %Y}):",
        "",
    ]
    last_expiry: Optional[date] = None
    for o in options:
        if o.expiry != last_expiry:
            days_left = (o.expiry - today).days
            lines.append(f"\U0001f4c5 {o.expiry:%a %d %b} · {days_left}d left")
            last_expiry = o.expiry
        lines.append(
            f"   • {o.broker}: {o.label} ×{_fmt_qty(o.net_qty)} ({o.side})"
        )
    lines.append("")
    lines.append("Roll or close before expiry to avoid assignment/expiry.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration + CLI
# --------------------------------------------------------------------------- #

def run_expiry_alert(
    client,
    *,
    notifier: Callable[[str], bool] = notify_safe,
    today: Optional[date] = None,
    within_days: int = DEFAULT_WITHIN_DAYS,
) -> tuple[list[ExpiringOption], bool]:
    """Find expiring options, send the digest, return ``(options, delivered)``."""
    today = today or date.today()
    options = find_expiring_options(client, today=today, within_days=within_days)
    delivered = notifier(format_expiry_message(options, today=today, within_days=within_days))
    return options, delivered


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from config.settings import get_service_account_info, get_spreadsheet_id
    from sheets.writer import SheetClient

    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    options, delivered = run_expiry_alert(client)

    log.info(
        "Options-expiry alert: %d contract(s) expiring within %d days; delivered=%s",
        len(options), DEFAULT_WITHIN_DAYS, delivered,
    )
    return 0 if delivered else 1


# --------------------------------------------------------------------------- #
# Cell helpers (shared shape with lemon8.reader)
# --------------------------------------------------------------------------- #

def _cell(row: list[Any], i: int) -> Any:
    return row[i] if i < len(row) else ""


def _dec(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(val: Any) -> Optional[date]:
    """Parse a sheet cell to a date. Expiry is stored ISO (``yyyy-mm-dd``); a bare
    serial is tolerated in case the cell is read unformatted."""
    if val is None or val == "":
        return None
    s = str(val).strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    try:
        return date(1899, 12, 30) + timedelta(days=int(float(s)))
    except (ValueError, OverflowError):
        return None


def _fmt_qty(qty: Decimal) -> str:
    q = abs(qty)
    return f"{q:.0f}" if q == q.to_integral_value() else f"{q.normalize():f}"


if __name__ == "__main__":
    raise SystemExit(main())

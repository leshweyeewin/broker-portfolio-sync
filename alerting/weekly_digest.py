"""Weekly options digest — pushes earnings credit-spread and wheel scans to Telegram.

Read-only: plans/scores only, never orders. Mirrors ``run.py --analytics`` — it
builds a report and pushes it through the safe notify path (``notify_safe``, which
never raises and auto-splits long messages). Intended to run on a weekly schedule
(cron / Task Scheduler / Docker), separate from the daily sync.

Run it with::

    python -m alerting.weekly_digest
"""

from __future__ import annotations

import contextlib
import io
import logging
from datetime import date
from typing import Callable, Optional

from alerting.notify import notify_safe

log = logging.getLogger(__name__)

_SEP = "=" * 40


def build_spreads_digest(*, today: Optional[date] = None) -> str:
    """Earnings IV-crush credit-spread scan over the watchlist + held names."""
    from analytics.earnings.iv_crush import scan_iv_crush, format_message, earnings_universe

    universe = earnings_universe()
    if not universe:
        return "📅 IV-Crush: no earnings watchlist configured."
    cands = scan_iv_crush(universe, today=today)
    if not cands:
        return "📅 IV-Crush: no upcoming earnings in horizon."
    return format_message(cands)


def build_wheel_digest() -> str:
    """CSP / covered-call / PMCC scan from current Sheet positions (captured stdout)."""
    from analytics.options.income_workspace import main as wheel_main

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        wheel_main([])
    return buf.getvalue().strip() or "💼 Wheel: no active positions."


def run_weekly_digest(
    *,
    notifier: Callable[[str], bool] = notify_safe,
    today: Optional[date] = None,
) -> str:
    """Build both scans, push the combined digest, and return the message sent.

    ``notifier`` is injectable so tests drive it offline without touching Telegram.
    """
    spreads = build_spreads_digest(today=today)
    wheel = build_wheel_digest()
    message = (
        "🗓️ WEEKLY OPTIONS DIGEST (read-only — plans only, no orders)\n\n"
        f"{spreads}\n\n{_SEP}\n{wheel}"
    )
    notifier(message)
    return message


def main(argv=None) -> int:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    message = run_weekly_digest()
    log.info("Weekly digest pushed (%d chars).", len(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

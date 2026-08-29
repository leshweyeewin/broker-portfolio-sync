"""Post-earnings IV-crush history — playbook Steps 2 & 3.

Turns the daily ATM-IV snapshots accumulated by ``analytics.earnings.iv_logger`` into the
two middle steps of the moomoo IV-Crush grade:

  * **Step 2 — Consistent Crush**: how reliably ATM IV *fell* right after past
    earnings (fraction of measurable events with a positive crush).
  * **Step 3 — Double-Digit Crush**: the average size of that post-earnings IV drop.

One event's crush = ``(iv_pre - iv_post) / iv_pre * 100``, where ``iv_pre`` is the
latest snapshot on/before the earnings date and ``iv_post`` is the earliest
snapshot in the few days after it. Positive = IV crushed (the edge). Both sides
must exist or the event is skipped.

The measurement core (:func:`measure_iv_crush`) is pure and offline-testable; the
only I/O is reading the snapshot file and the earnings calendar in
:func:`historical_iv_crush`.

NOTE: ``iv_history.json`` only starts accumulating the day the logger first runs,
so every ticker returns ``n == 0`` / ``None`` until at least one earnings date has
been straddled by logged snapshots (weeks to a quarter of daily logging). Callers
must render "n/a" gracefully.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from analytics.earnings.earnings import get_earnings_dates

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK = 8
DEFAULT_POST_WINDOW_DAYS = 3  # how many days after earnings the post-crush snapshot may fall


@dataclass
class CrushStudy:
    """Aggregate post-earnings IV-crush statistics for one ticker."""
    n: int                              # measurable events
    consistency: Optional[float]        # fraction 0..1 where IV fell, None if n == 0
    avg_crush_pct: Optional[float]      # mean crush %, positive = IV fell
    events: list[tuple[date, float]]    # (earnings_date, crush_pct)


def _parse_history(iv_by_date: dict[str, float]) -> list[tuple[date, float]]:
    """Parse ``{iso_date: iv}`` into an ascending ``[(date, iv)]`` list."""
    out: list[tuple[date, float]] = []
    for d, iv in iv_by_date.items():
        try:
            out.append((date.fromisoformat(d), float(iv)))
        except (ValueError, TypeError):
            continue
    out.sort(key=lambda t: t[0])
    return out


def measure_iv_crush(
    iv_by_date: dict[str, float],
    earnings_dates: Sequence[date],
    *,
    post_window_days: int = DEFAULT_POST_WINDOW_DAYS,
) -> CrushStudy:
    """Pair pre/post-earnings IV snapshots and aggregate. Pure + offline.

    ``iv_by_date`` is one ticker's snapshot history (``{iso_date: iv_fraction}``).
    An earnings date with no snapshot on/before it, or none within
    ``post_window_days`` after it, is skipped (not measurable).
    """
    series = _parse_history(iv_by_date)
    events: list[tuple[date, float]] = []
    for ed in sorted(set(earnings_dates)):
        # iv_pre: latest snapshot on/before the earnings date (series is ascending).
        pre: Optional[float] = None
        for d, iv in series:
            if d <= ed:
                pre = iv
            else:
                break
        # iv_post: earliest snapshot strictly after ed, within the window.
        post: Optional[float] = None
        for d, iv in series:
            if ed < d <= ed + timedelta(days=post_window_days):
                post = iv
                break
        if pre is None or post is None or pre <= 0:
            continue
        events.append((ed, round((pre - post) / pre * 100, 2)))

    if not events:
        return CrushStudy(n=0, consistency=None, avg_crush_pct=None, events=[])
    n = len(events)
    dropped = sum(1 for _, c in events if c > 0)
    avg = sum(c for _, c in events) / n
    return CrushStudy(
        n=n,
        consistency=round(dropped / n, 2),
        avg_crush_pct=round(avg, 2),
        events=events,
    )


def historical_iv_crush(
    ticker: str,
    *,
    lookback: int = DEFAULT_LOOKBACK,
    today: Optional[date] = None,
) -> Optional[CrushStudy]:
    """Study a ticker's post-earnings IV crush from logged snapshots (best-effort).

    Returns ``None`` when there is no logged IV history or no past earnings dates.
    The pure work is in :func:`measure_iv_crush`.
    """
    from analytics.earnings.iv_crush import _load_iv_history  # lazy: avoids an import cycle

    hist = _load_iv_history(ticker)
    if not hist:
        return None
    today = today or date.today()
    past = [d for d in get_earnings_dates(ticker) if d < today]
    if not past:
        return None
    past = sorted(past)[-lookback:]
    return measure_iv_crush(hist, past)

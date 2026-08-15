"""Tests for alerting/weekly_pl_alert.py — the Sunday realized-P/L digest.

Offline: closed trades are built as ClosedPosition values and delivery goes
through an injected notifier / a monkeypatched reader.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from alerting import weekly_pl_alert as wpl
from alerting.weekly_pl_alert import (
    BrokerWeekPL,
    compute_weekly_pl,
    format_weekly_pl_message,
    run_weekly_pl_alert,
)
from lemon8.reader import ClosedPosition

_TODAY = date(2026, 8, 15)   # Saturday → ISO week Mon 2026-08-10 .. today


def _closed(broker, pl_sgd, *, close="2026-08-13"):
    return ClosedPosition(
        broker=broker, symbol="X", asset="option", close_date=close, currency="USD",
        realized_pl=Decimal(pl_sgd) if pl_sgd is not None else None,
        realized_pl_sgd=Decimal(pl_sgd) if pl_sgd is not None else None,
        return_pct=None,
    )


# --------------------------------------------------------------------------- #
# compute_weekly_pl
# --------------------------------------------------------------------------- #

def test_groups_by_broker_and_sums_this_week():
    closed = [
        _closed("Tiger", 100),
        _closed("Tiger", -40),
        _closed("Longbridge", 25),
        _closed("Tiger", 10, close="2026-08-01"),   # last week → excluded
        _closed("MooMoo", None),                     # no booked P/L → excluded
    ]
    out = compute_weekly_pl(closed, today=_TODAY)
    by = {r.broker: r for r in out}
    assert by["Tiger"].pl_sgd == Decimal(60)
    assert by["Tiger"].trades == 2 and by["Tiger"].wins == 1
    assert by["Longbridge"].pl_sgd == Decimal(25)
    assert "MooMoo" not in by
    # sorted by P/L desc → Tiger (60) before Longbridge (25)
    assert [r.broker for r in out] == ["Tiger", "Longbridge"]


def test_win_rate():
    assert BrokerWeekPL("Tiger", Decimal(0), 3, 2).win_rate == 67
    assert BrokerWeekPL("Tiger", Decimal(0), 0, 0).win_rate == 0


# --------------------------------------------------------------------------- #
# format_weekly_pl_message
# --------------------------------------------------------------------------- #

def test_message_shows_brokers_total_and_week():
    results = [BrokerWeekPL("Tiger", Decimal("14509.89"), 63, 40),
               BrokerWeekPL("Longbridge", Decimal("51.64"), 1, 1)]
    msg = format_weekly_pl_message(results, today=_TODAY)
    assert "10 Aug – 15 Aug 2026" in msg
    assert "Tiger: +14,509.89 SGD" in msg
    assert "Total realized: +14,561.53 SGD  (64 trades)" in msg


def test_message_empty_week():
    msg = format_weekly_pl_message([], today=_TODAY)
    assert "No trades closed this week." in msg


# --------------------------------------------------------------------------- #
# run_weekly_pl_alert
# --------------------------------------------------------------------------- #

def test_run_sends_and_reports_delivery(monkeypatch):
    monkeypatch.setattr(wpl, "read_closed_positions",
                        lambda client: [_closed("Tiger", 100), _closed("Tiger", -30)])
    sent = {}
    def notifier(text):
        sent["text"] = text
        return True
    results, delivered = run_weekly_pl_alert(object(), notifier=notifier, today=_TODAY)
    assert delivered is True
    assert results[0].broker == "Tiger" and results[0].pl_sgd == Decimal(70)
    assert "Weekly P/L" in sent["text"]

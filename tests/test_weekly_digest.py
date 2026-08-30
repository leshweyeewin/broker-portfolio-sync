"""Offline tests for the weekly options digest (``alerting.weekly_digest``).

The two scans are monkeypatched and the notifier is captured, so the composition
and safe-push wiring are exercised without touching yfinance, the Sheet, or
Telegram.
"""

from __future__ import annotations

import alerting.weekly_digest as wd


def test_run_weekly_digest_composes_and_pushes(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(wd, "build_spreads_digest", lambda today=None: "SPREADS BLOCK")
    monkeypatch.setattr(wd, "build_wheel_digest", lambda: "WHEEL BLOCK")

    msg = wd.run_weekly_digest(notifier=lambda text: sent.append(text) or True)

    assert sent == [msg]                       # pushed exactly what it returned
    assert "WEEKLY OPTIONS DIGEST" in msg
    assert "plans only, no orders" in msg       # read-only boundary is stated
    assert "SPREADS BLOCK" in msg and "WHEEL BLOCK" in msg


def test_build_spreads_digest_empty_universe(monkeypatch):
    import analytics.earnings.iv_crush as ivc
    monkeypatch.setattr(ivc, "earnings_universe", lambda: [])
    assert "no earnings watchlist" in wd.build_spreads_digest().lower()

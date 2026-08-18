"""Tests for analytics.risk_engine — expiry risk engine with playbook signals."""

from datetime import date
from decimal import Decimal

import pytest

from adapters.base import Broker, OptionAction, OptionTrade, OptionType
from analytics.risk_engine import (
    Signal,
    format_risk_alert_message,
    generate_risk_alerts,
)


def _option(underlying="AAPL", action="Sell", d="2025-06-15", fill_id="o1",
            expiry="2025-06-20", strike="150"):
    return OptionTrade(
        date=date.fromisoformat(d),
        broker=Broker.TIGER,
        underlying=underlying,
        option_type=OptionType.PUT,
        strike=Decimal(strike),
        qty=Decimal("1"),
        expiry=date.fromisoformat(expiry),
        action=OptionAction(action),
        premium=Decimal("3.50"),
        fee=Decimal("0.65"),
        currency="USD",
        strategy="",
        dedup_key=f"Tiger:{fill_id}",
    )


def _td(trade, status="Open", pl=None):
    raw = [""] * 18
    if pl is not None:
        raw[14] = float(pl)
    return {"trade": trade, "status": status, "raw": raw}


class TestRiskAlerts:
    def test_no_open_options(self):
        """No open options → no alerts."""
        alerts = generate_risk_alerts([], today=date(2025, 6, 15))
        assert alerts == []

    def test_earnings_close_signal(self):
        """Earnings play with 1 day to expiry → CLOSE_POSITION."""
        from unittest.mock import patch
        opt = _option(expiry="2025-06-16")
        trades = [_td(opt)]
        tags = {opt.dedup_key: "Earnings IV Crush"}

        with patch("analytics.earnings.get_earnings_dates", return_value=[date(2025, 6, 16)]):
            alerts = generate_risk_alerts(trades, tags, today=date(2025, 6, 15))
        assert len(alerts) == 1
        assert alerts[0].signal == Signal.CLOSE_POSITION

    def test_earnings_roll_on_loss(self):
        """Earnings play with loss and 5 days left → ROLL_SPREAD."""
        from unittest.mock import patch
        opt = _option(expiry="2025-06-20")
        trades = [_td(opt, pl=-100)]
        tags = {opt.dedup_key: "Earnings IV Crush"}

        with patch("analytics.earnings.get_earnings_dates", return_value=[date(2025, 6, 16)]):
            alerts = generate_risk_alerts(trades, tags, today=date(2025, 6, 15))
        assert len(alerts) == 1
        assert alerts[0].signal == Signal.ROLL_SPREAD

    def test_cut_trade_on_loss_near_expiry(self):
        """Non-earnings play at a loss with 3 days left → CUT_TRADE."""
        opt = _option(expiry="2025-06-18")
        trades = [_td(opt, pl=-50)]
        tags = {}  # no earnings tag

        alerts = generate_risk_alerts(trades, tags, today=date(2025, 6, 15))
        assert len(alerts) == 1
        assert alerts[0].signal == Signal.CUT_TRADE

    def test_roll_timeline_near_expiry(self):
        """Non-earnings play, no loss, 2 days left → ROLL_TIMELINE."""
        opt = _option(expiry="2025-06-17")
        trades = [_td(opt)]
        tags = {}

        alerts = generate_risk_alerts(trades, tags, today=date(2025, 6, 15))
        assert len(alerts) == 1
        assert alerts[0].signal == Signal.ROLL_TIMELINE

    def test_needs_review_with_time(self):
        """Position with 10 days, no loss → NEEDS_REVIEW."""
        opt = _option(expiry="2025-06-25")
        trades = [_td(opt)]
        tags = {}

        alerts = generate_risk_alerts(trades, tags, today=date(2025, 6, 15))
        assert len(alerts) == 1
        assert alerts[0].signal == Signal.NEEDS_REVIEW

    def test_outside_window_ignored(self):
        """Positions with > 14 DTE or < 1 DTE are excluded."""
        far_opt = _option(expiry="2025-07-15")
        expired_opt = _option(expiry="2025-06-14")
        trades = [_td(far_opt), _td(expired_opt)]

        alerts = generate_risk_alerts(trades, today=date(2025, 6, 15))
        assert alerts == []

    def test_closed_positions_ignored(self):
        """Closed positions should not generate alerts."""
        opt = _option(expiry="2025-06-17")
        trades = [_td(opt, status="Closed")]

        alerts = generate_risk_alerts(trades, today=date(2025, 6, 15))
        assert alerts == []


class TestFormatMessage:
    def test_no_alerts(self):
        msg = format_risk_alert_message([], today=date(2025, 6, 15))
        assert "No open options" in msg

    def test_with_alerts(self):
        opt = _option(expiry="2025-06-17")
        trades = [_td(opt)]
        alerts = generate_risk_alerts(trades, today=date(2025, 6, 15))
        msg = format_risk_alert_message(alerts, today=date(2025, 6, 15))
        assert "AAPL" in msg
        assert "150" in msg

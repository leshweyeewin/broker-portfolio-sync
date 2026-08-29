"""Tests for analytics.risk.diagnostics — the three diagnostic calculators."""

from datetime import date
from decimal import Decimal

import pytest

from adapters.base import Broker, StockAction, StockTrade
from analytics.risk.diagnostics import (
    earnings_iv_crush_analysis,
    intraday_fee_drag,
    medium_term_performance,
)
from analytics.screening.tagger import TAG_DAY_TRADE, TAG_EARNINGS_IV_CRUSH, TAG_MEDIUM_TERM


def _stock(ticker="AAPL", action="Sell", d="2025-06-15", fill_id="s1", fee="1.00"):
    return StockTrade(
        date=date.fromisoformat(d),
        broker=Broker.TIGER,
        ticker=ticker,
        action=StockAction(action),
        qty=Decimal("10"),
        price=Decimal("150"),
        fee=Decimal(fee),
        currency="USD",
        dedup_key=f"Tiger:{fill_id}",
    )


def _td(trade, status="Closed", pl_sgd=None):
    """Build a trade dict with optional P/L in the raw row."""
    raw = [""] * 16
    if pl_sgd is not None:
        raw[11] = float(pl_sgd)  # Realized P/L (SGD) position for stocks
    return {"trade": trade, "status": status, "raw": raw}


# --------------------------------------------------------------------------- #
# Earnings IV Crush Analyzer
# --------------------------------------------------------------------------- #

class TestIVCrushAnalysis:
    def test_no_trades(self):
        result = earnings_iv_crush_analysis([])
        assert result.win_count == 0
        assert result.loss_count == 0
        assert not result.risk_warning

    def test_wins_and_losses(self):
        trades = [
            (_td(_stock(fill_id="s1"), pl_sgd=50), TAG_EARNINGS_IV_CRUSH),
            (_td(_stock(fill_id="s2"), pl_sgd=30), TAG_EARNINGS_IV_CRUSH),
            (_td(_stock(fill_id="s3"), pl_sgd=-200), TAG_EARNINGS_IV_CRUSH),
        ]
        result = earnings_iv_crush_analysis(trades)
        assert result.win_count == 2
        assert result.loss_count == 1
        assert result.avg_win == Decimal("40")  # (50 + 30) / 2
        assert result.avg_loss == Decimal("200")
        assert result.risk_warning  # 200 > 2 * 40

    def test_no_risk_warning_when_balanced(self):
        trades = [
            (_td(_stock(fill_id="s1"), pl_sgd=100), TAG_EARNINGS_IV_CRUSH),
            (_td(_stock(fill_id="s2"), pl_sgd=-50), TAG_EARNINGS_IV_CRUSH),
        ]
        result = earnings_iv_crush_analysis(trades)
        assert not result.risk_warning  # 50 < 2 * 100

    def test_ignores_non_earnings_trades(self):
        trades = [
            (_td(_stock(fill_id="s1"), pl_sgd=100), TAG_DAY_TRADE),
            (_td(_stock(fill_id="s2"), pl_sgd=50), TAG_EARNINGS_IV_CRUSH),
        ]
        result = earnings_iv_crush_analysis(trades)
        assert result.win_count == 1  # only the earnings trade


# --------------------------------------------------------------------------- #
# Fee Drag Calculator
# --------------------------------------------------------------------------- #

class TestFeeDrag:
    def test_no_day_trades(self):
        result = intraday_fee_drag([])
        assert result.trade_count == 0
        assert not result.alert

    def test_high_fee_drag(self):
        """Fees > 15% of gross profit → alert."""
        trades = [
            (_td(_stock(fill_id="s1", fee="20"), pl_sgd=50), TAG_DAY_TRADE),
            (_td(_stock(fill_id="s2", fee="15"), pl_sgd=30), TAG_DAY_TRADE),
        ]
        result = intraday_fee_drag(trades)
        assert result.trade_count == 2
        assert result.total_fees == Decimal("35")
        # Gross profit is extracted from raw P/L, not fee
        assert result.alert  # 35 / 80 = 43.75% > 15%

    def test_low_fee_drag(self):
        """Fees < 15% → no alert."""
        trades = [
            (_td(_stock(fill_id="s1", fee="1"), pl_sgd=500), TAG_DAY_TRADE),
        ]
        result = intraday_fee_drag(trades)
        assert not result.alert


# --------------------------------------------------------------------------- #
# Medium-Term Performance
# --------------------------------------------------------------------------- #

class TestMediumTermPerformance:
    def test_no_trades(self):
        result = medium_term_performance([])
        assert result.trade_count == 0

    def test_return_calculation(self):
        trades = [
            (_td(_stock(fill_id="s1"), pl_sgd=100), TAG_MEDIUM_TERM),
            (_td(_stock(fill_id="s2"), pl_sgd=200), TAG_MEDIUM_TERM),
        ]
        result = medium_term_performance(trades, total_capital_sgd=Decimal("10000"))
        assert result.trade_count == 2
        assert result.total_pl_sgd == Decimal("300")
        assert result.total_return_pct == Decimal("3")  # 300 / 10000 * 100

    def test_no_capital_no_pct(self):
        trades = [
            (_td(_stock(fill_id="s1"), pl_sgd=100), TAG_MEDIUM_TERM),
        ]
        result = medium_term_performance(trades)
        assert result.total_return_pct == Decimal("0")

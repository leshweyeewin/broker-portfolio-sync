from datetime import date
from decimal import Decimal
from analytics.reporting.report import (
    AnalyticsReport,
    format_telegram_report,
    run_analytics,
)
from analytics.risk.diagnostics import FeeDragResult, IVCrushResult, MediumTermResult
from analytics.screening.market_scan import TickerMover, UpcomingEarnings
from analytics.screening.screener import ScreenerResult
from analytics.screening.swing import SwingSetup

def test_format_telegram_report_empty():
    report = AnalyticsReport()
    msg = format_telegram_report(report, today=date(2024, 1, 1))
    assert "Daily Market & Portfolio Report" in msg
    assert "1 Jan 2024" in msg
    # Because it's empty, some specific fields shouldn't be there
    assert "Daily Ticker Movers" not in msg

def test_format_telegram_report_with_data():
    report = AnalyticsReport(
        bullish_movers=[TickerMover("NVDA", 100.0, 50.0, 5.0)],
        bearish_movers=[TickerMover("TSLA", 100.0, -10.0, -1.0)],
        upcoming_earnings=[UpcomingEarnings("AAPL", date(2024, 1, 10), "Q4", 5.0)],
        screener_picks=[
            ScreenerResult("NVDA", "2024-02-15", "Put", Decimal("500"), Decimal("5.0"), Decimal("5.2"), Decimal("0.2"), 0.35, 0.5, 80, 1000, 100, Decimal("5.1")),
            ScreenerResult("TSLA", "2024-02-15", "Call", Decimal("200"), Decimal("2.0"), Decimal("2.2"), Decimal("0.2"), 0.35, 0.5, 80, 1000, 100, Decimal("2.1"))
        ],
        swing_setups=[
            SwingSetup("AAPL", 150.0, "Breakout", rsi14=65.0, atr_pct=5.0)
        ],
        fee_drag=FeeDragResult(alert=True, alert_message="High fee drag warning"),
        iv_crush=IVCrushResult(risk_warning=True, warning_message="IV crush risk warning")
    )
    
    msg = format_telegram_report(report, today=date(2024, 1, 1))
    
    assert "Daily Ticker Movers" in msg
    assert "NVDA" in msg
    assert "TSLA" in msg
    assert "Upcoming Earnings" in msg
    assert "Systematic Short Option Picks" in msg
    assert "Bullish Income (Short Put)" in msg
    assert "Bearish Income (Short Call)" in msg
    assert "Swing Setups" in msg
    assert "High fee drag warning" in msg
    assert "IV crush risk warning" in msg

def test_run_analytics(monkeypatch):
    class MockWriter:
        def read_all_stock_trades(self):
            return []
        def read_all_option_trades(self):
            return []
            
    # Mock scanning functions to prevent external calls
    monkeypatch.setattr("analytics.reporting.report.get_daily_movers", lambda x: ([], []))
    monkeypatch.setattr("analytics.reporting.report.get_upcoming_earnings", lambda *args, **kwargs: [])
    monkeypatch.setattr("analytics.reporting.report.scan_short_option_picks", lambda *args, **kwargs: [])
    monkeypatch.setattr("analytics.reporting.report.scan_swing_setups", lambda *args, **kwargs: [])
    
    # Mock tags
    monkeypatch.setattr("analytics.reporting.report.tag_stock_trades", lambda x: {})
    monkeypatch.setattr("analytics.reporting.report.tag_option_trades", lambda x: {})
    
    report = run_analytics(MockWriter(), today=date(2024, 1, 1))
    
    assert isinstance(report, AnalyticsReport)
    assert not report.bullish_movers
    assert not report.bearish_movers
    assert not report.upcoming_earnings
    assert not report.screener_picks
    assert not report.swing_setups

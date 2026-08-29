import pytest
from decimal import Decimal
from datetime import date
from alerting.plan_alerts import evaluate_plans, PlanAlert
from analytics.options.trade_plans import TradePlan, PlanStatus
from analytics.options.payoff import OptionLeg

class DummyQuoteClient:
    def get_option_chain(self, *args, **kwargs):
        pass

def test_evaluate_plans():
    # This test will mock out the yfinance price fetch to avoid network calls.
    yf = pytest.importorskip("yfinance", reason="yfinance not installed")
    
    class DummyFastInfo:
        last_price = 195.0
        
    class DummyTicker:
        def __init__(self, ticker):
            self.fast_info = DummyFastInfo()
            
    # Monkeypatch yf.Ticker for the test
    original_ticker = yf.Ticker
    yf.Ticker = DummyTicker
    
    try:
        plan1 = TradePlan(
            ticker="AAPL",
            strategy="Long Call",
            legs=[OptionLeg(right="call", side="buy", strike=Decimal("200"), premium=Decimal("5"), expiry=date(2026, 9, 18))],
            bias="bullish",
            entry_trigger="Price hits 190 target",
            exit_rule="Take profit at 50%",
            status=PlanStatus.APPROVED,
            risk_budget=Decimal("500")
        )
        
        plan2 = TradePlan(
            ticker="TSLA",
            strategy="Short Put",
            legs=[OptionLeg(right="put", side="sell", strike=Decimal("150"), premium=Decimal("3"), expiry=date(2026, 9, 18))],
            bias="bullish",
            entry_trigger="",
            invalidation="Stop loss if drops below 140",
            exit_rule="Profit at 50%",
            status=PlanStatus.ENTERED,
            risk_budget=Decimal("100")
        )
        
        # Should not fire since it's closed
        plan3 = TradePlan(
            ticker="MSFT",
            strategy="Long Call",
            legs=[OptionLeg(right="call", side="buy", strike=Decimal("400"), premium=Decimal("5"), expiry=date(2026, 9, 18))],
            bias="bullish",
            entry_trigger="target 390",
            status=PlanStatus.CLOSED
        )
        
        client = DummyQuoteClient()
        alerts = evaluate_plans([plan1, plan2, plan3], client)
        
        assert len(alerts) == 2
        
        a1 = next((a for a in alerts if a.plan.ticker == "AAPL"), None)
        assert a1 is not None
        assert "Entry target price condition may be met" in a1.reason
        
        a2 = next((a for a in alerts if a.plan.ticker == "TSLA"), None)
        assert a2 is not None
        assert "Take-profit / Exit condition may be met" in a2.reason
        
    finally:
        yf.Ticker = original_ticker

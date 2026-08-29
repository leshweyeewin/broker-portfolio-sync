import pytest
from decimal import Decimal
from datetime import date
from analytics.options.portfolio_risk import calculate_portfolio_risk, RiskGuardrails
from core.fifo_pl import Holding
from adapters.base import OptionType

def test_calculate_portfolio_risk():
    holdings = [
        # Short Put CSP (Assignment risk)
        Holding(broker="tiger", instrument="AAPL 2026-09-18 150 put", symbol="AAPL", qty=Decimal("-2"), avg_price=Decimal("2.5"), open_fees=Decimal("0"), currency="USD", multiplier=Decimal("100"), option_type=OptionType.PUT, strike=Decimal("150"), expiry=date(2026, 9, 18)),
        # Short Call (Unlimited risk)
        Holding(broker="tiger", instrument="TSLA 2026-09-18 300 call", symbol="TSLA", qty=Decimal("-1"), avg_price=Decimal("5.0"), open_fees=Decimal("0"), currency="USD", multiplier=Decimal("100"), option_type=OptionType.CALL, strike=Decimal("300"), expiry=date(2026, 9, 18)),
        # Long Call (Defined risk)
        Holding(broker="tiger", instrument="MSFT 2026-09-18 400 call", symbol="MSFT", qty=Decimal("3"), avg_price=Decimal("10.0"), open_fees=Decimal("0"), currency="USD", multiplier=Decimal("100"), option_type=OptionType.CALL, strike=Decimal("400"), expiry=date(2026, 9, 18)),
        # Closed position (Should be ignored)
        Holding(broker="tiger", instrument="NVDA 2026-09-18 120 put", symbol="NVDA", qty=Decimal("0"), avg_price=Decimal("3.0"), open_fees=Decimal("0"), currency="USD", multiplier=Decimal("100"), option_type=OptionType.PUT, strike=Decimal("120"), expiry=date(2026, 9, 18))
    ]
    
    aum = Decimal("100000")
    guardrails = RiskGuardrails(max_aggregate_risk_pct=Decimal("0.25"))
    
    metrics = calculate_portfolio_risk(holdings, aum, guardrails)
    
    assert metrics.total_assignment_notional == Decimal("30000")
    assert metrics.aggregate_max_loss == Decimal("33000")
    
    assert metrics.concentration_by_underlying["AAPL"] == 2
    assert metrics.concentration_by_underlying["TSLA"] == 1
    assert metrics.concentration_by_underlying["MSFT"] == 3
    assert "NVDA" not in metrics.concentration_by_underlying
    
    assert any("UNLIMITED RISK" in w and "TSLA" in w for w in metrics.guardrail_warnings)
    assert any("Aggregate risk (33.0%) exceeds maximum (25.0%)" in w for w in metrics.guardrail_warnings)

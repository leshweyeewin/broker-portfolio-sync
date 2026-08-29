import pytest
from decimal import Decimal
from datetime import date
from core.fifo_pl import Realization
from analytics.options.trade_plans import TradePlan, PlanStatus
from analytics.options.payoff import OptionLeg
from analytics.options.journal import generate_journal

def test_generate_journal():
    plans = [
        TradePlan(
            ticker="AAPL",
            strategy="Long Call",
            legs=[OptionLeg(right="call", side="buy", strike=Decimal("200"), premium=Decimal("5"), expiry=date(2026, 9, 18))],
            bias="bullish",
            entry_trigger="target 190",
            exit_rule="target 50%",
            status=PlanStatus.CLOSED
        ),
        TradePlan(
            ticker="TSLA",
            strategy="Long Put",
            legs=[OptionLeg(right="put", side="buy", strike=Decimal("150"), premium=Decimal("3"), expiry=date(2026, 9, 18))],
            bias="bearish",
            invalidation="stop loss",
            status=PlanStatus.CLOSED
        )
    ]
    
    realizations = [
        # Profitable
        Realization(key="r1", broker="tiger", instrument="AAPL 2026-09-18 200 call", date=date(2026, 8, 20), qty=Decimal("1"), realized_pl=Decimal("250.0"), close_value=Decimal("750.0"), cost_basis=Decimal("500.0"), open_fees=Decimal("0"), close_fee=Decimal("0"), currency="USD", multiplier=Decimal("100")),
        # Loss
        Realization(key="r2", broker="tiger", instrument="TSLA 2026-09-18 150 put", date=date(2026, 8, 21), qty=Decimal("1"), realized_pl=Decimal("-150.0"), close_value=Decimal("150.0"), cost_basis=Decimal("300.0"), open_fees=Decimal("0"), close_fee=Decimal("0"), currency="USD", multiplier=Decimal("100")),
        # Unmatched
        Realization(key="r3", broker="tiger", instrument="NVDA 2026-09-18 100 call", date=date(2026, 8, 22), qty=Decimal("1"), realized_pl=Decimal("100.0"), close_value=Decimal("300.0"), cost_basis=Decimal("200.0"), open_fees=Decimal("0"), close_fee=Decimal("0"), currency="USD", multiplier=Decimal("100")),
    ]
    
    entries = generate_journal(plans, realizations)
    
    # NVDA should be ignored
    assert len(entries) == 2
    
    # Should be sorted newest first (TSLA then AAPL)
    assert entries[0].plan.ticker == "TSLA"
    assert entries[0].grade == "B"  # Hit invalidation rule
    assert "Loss realized: -$150.00" in entries[0].notes
    
    assert entries[1].plan.ticker == "AAPL"
    assert entries[1].grade == "A"
    assert "Profitable close: +$250.00" in entries[1].notes

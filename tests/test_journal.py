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


def _real(key, instrument, d, pl):
    return Realization(key=key, broker="tiger", instrument=instrument, date=d, qty=Decimal("1"),
                       realized_pl=Decimal(pl), close_value=Decimal("0"), cost_basis=Decimal("0"),
                       open_fees=Decimal("0"), close_fee=Decimal("0"), currency="USD", multiplier=Decimal("100"))


def test_scorecard_groups_by_rule_and_flags_unplanned_trades():
    from analytics.options.journal import NO_PLAN, format_scorecard, scorecard_by_rule
    leg = [OptionLeg(right="put", side="sell", strike=Decimal("95"), premium=Decimal("1"), expiry=date(2026, 9, 18))]
    plans = [TradePlan(ticker="AMZN", strategy="PCS", legs=leg, status=PlanStatus.CLOSED, rule_name="weekly_income_fri")]
    reals = [
        _real("a", "AMZN 2026-09-18 95 put", date(2026, 8, 1), "30"),
        _real("b", "AMZN 2026-09-18 95 put", date(2026, 8, 8), "30"),
        _real("c", "AMZN 2026-09-18 95 put", date(2026, 8, 15), "-100"),
        _real("d", "SNDQ 2026-09-18 10 put", date(2026, 8, 20), "-500"),
    ]
    by = {s.rule: s for s in scorecard_by_rule(plans, reals)}
    w = by["weekly_income_fri"]
    assert (w.trades, w.total_pl, w.win_rate) == (3, Decimal("-40"), pytest.approx(2 / 3))
    assert w.max_drawdown == Decimal("100")
    assert w.wins_erased_by_one_loss == pytest.approx(100 / 30)
    assert any("erases" in x for x in w.warnings) and any("noise" in x for x in w.warnings)
    assert by[NO_PLAN].total_pl == Decimal("-500") and any("no saved plan" in x for x in by[NO_PLAN].warnings)
    out = format_scorecard(list(by.values()))
    assert "weekly_income_fri" in out and "(no plan)" in out

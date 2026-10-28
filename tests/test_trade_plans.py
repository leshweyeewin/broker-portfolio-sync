from datetime import date
from decimal import Decimal

import pytest

from analytics.options.payoff import bull_call_spread, short_call
from analytics.options.trade_plans import PlanStatus, TradePlan, TradePlanStore, transition, validate_plan


def _complete_plan():
    return TradePlan(
        ticker="AAPL", strategy="Bull Call Spread", legs=bull_call_spread(200, 5, 210, 2, expiry=date(2026, 9, 18)),
        thesis="breakout", entry_trigger="close above 200", invalidation="below 195", exit_rule="50% target",
        risk_budget="400", snapshot_id="snapshot-1", second_leg_purpose="fund",
    )


def test_drafts_can_be_incomplete_but_approval_requires_rules_and_budget():
    draft = TradePlan("AAPL", "Long Call", ())
    assert any(issue.field == "legs" for issue in validate_plan(draft))
    with pytest.raises(ValueError, match="cannot approve"):
        transition(draft, PlanStatus.APPROVED)


def test_approval_checks_bounded_loss_and_budget_then_valid_transitions():
    plan = _complete_plan()
    assert not [i for i in validate_plan(plan, for_approval=True) if i.severity == "error"]
    approved = transition(plan, PlanStatus.APPROVED)
    assert approved.status is PlanStatus.APPROVED
    assert transition(approved, PlanStatus.ENTERED).status is PlanStatus.ENTERED
    with pytest.raises(ValueError, match="cannot transition"):
        transition(approved, PlanStatus.CLOSED)
    unlimited = TradePlan("AAPL", "Short Call", (short_call(200, 2, expiry=date(2026, 9, 18)),), entry_trigger="x", invalidation="x", exit_rule="x", risk_budget=1000, snapshot_id="s")
    with pytest.raises(ValueError, match="unbounded"):
        transition(unlimited, PlanStatus.APPROVED)


def test_json_store_round_trip_preserves_notes_and_decimal_legs(tmp_path):
    store = TradePlanStore(tmp_path / "plans.json")
    plan = _complete_plan()
    saved = store.save(plan)
    restored = store.get(saved.plan_id)
    assert restored is not None
    assert restored.thesis == "breakout"
    assert restored.legs[0].premium == Decimal("5")
    assert restored.payoff.max_loss == Decimal("300")
    assert store.list()[0].plan_id == saved.plan_id


def test_multi_leg_plan_needs_second_leg_purpose_to_approve():
    from dataclasses import replace
    plan = replace(_complete_plan(), second_leg_purpose="")
    with pytest.raises(ValueError, match="second leg"):
        transition(plan, PlanStatus.APPROVED)
    with pytest.raises(ValueError, match="second_leg_purpose"):
        TradePlan("AAPL", "x", (), second_leg_purpose="hope")


def test_second_leg_purpose_round_trips(tmp_path):
    store = TradePlanStore(tmp_path / "p.json")
    saved = store.save(_complete_plan())
    assert store.get(saved.plan_id).second_leg_purpose == "fund"
    from dataclasses import replace
    named = store.save(replace(saved, rule_name="weekly_income_fri"))
    assert store.get(named.plan_id).rule_name == "weekly_income_fri"


def test_assignment_exposure_describes_short_legs_only():
    from analytics.options.payoff import put_credit_spread
    plan = TradePlan("AMZN", "Bull Put", put_credit_spread(95, 2, 94, 1, expiry=date(2026, 9, 18)))
    (line,) = plan.assignment_exposure
    assert "buy 100 shares @ 95" in line and "$9,500.00" in line

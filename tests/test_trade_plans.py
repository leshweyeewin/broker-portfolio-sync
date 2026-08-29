from datetime import date
from decimal import Decimal

import pytest

from analytics.payoff import bull_call_spread, short_call
from analytics.trade_plans import PlanStatus, TradePlan, TradePlanStore, transition, validate_plan


def _complete_plan():
    return TradePlan(
        ticker="AAPL", strategy="Bull Call Spread", legs=bull_call_spread(200, 5, 210, 2, expiry=date(2026, 9, 18)),
        thesis="breakout", entry_trigger="close above 200", invalidation="below 195", exit_rule="50% target",
        risk_budget="400", snapshot_id="snapshot-1",
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

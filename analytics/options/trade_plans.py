"""Validated, local persistence for user-authored option trade plans.

Plans are not broker orders and are deliberately stored separately from synced
executions.  The JSON store is suitable for a first implementation and keeps
user-written thesis/lesson fields under explicit plan ownership.
"""

from __future__ import annotations

import json
import argparse
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal, Sequence

from adapters.base import dec
from analytics.options.payoff import OptionLeg, PayoffSummary, summarize_expiry

PLAN_STORE = Path("analytics") / "data" / "trade_plans.json"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    ENTERED = "entered"
    MANAGED = "managed"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class TradePlan:
    ticker: str
    strategy: str
    legs: tuple[OptionLeg, ...]
    thesis: str = ""
    bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    entry_trigger: str = ""
    invalidation: str = ""
    exit_rule: str = ""
    risk_budget: Decimal | None = None
    catalyst_date: date | None = None
    snapshot_id: str = ""
    status: PlanStatus = PlanStatus.DRAFT
    lesson: str = ""
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        object.__setattr__(self, "strategy", self.strategy.strip())
        object.__setattr__(self, "legs", tuple(self.legs))
        if not self.ticker or not self.strategy:
            raise ValueError("ticker and strategy are required")
        if self.bias not in ("bullish", "bearish", "neutral"):
            raise ValueError("invalid bias")
        if self.risk_budget is not None:
            object.__setattr__(self, "risk_budget", dec(self.risk_budget))
            if self.risk_budget <= Decimal("0"):
                raise ValueError("risk_budget must be positive")

    @property
    def payoff(self) -> PayoffSummary | None:
        return summarize_expiry(self.legs) if self.legs else None


def validate_plan(plan: TradePlan, *, for_approval: bool = False) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if not plan.legs:
        issues.append(ValidationIssue("legs", "at least one option leg is required"))
        payoff = None
    else:
        try:
            payoff = plan.payoff
        except ValueError as exc:
            issues.append(ValidationIssue("legs", str(exc)))
            payoff = None
    if for_approval:
        for field in ("entry_trigger", "invalidation", "exit_rule", "snapshot_id"):
            if not getattr(plan, field):
                issues.append(ValidationIssue(field, f"{field.replace('_', ' ')} is required before approval"))
        if plan.risk_budget is None:
            issues.append(ValidationIssue("risk_budget", "risk budget is required before approval"))
        if any(leg.expiry is None for leg in plan.legs):
            issues.append(ValidationIssue("legs", "each leg needs an expiry before approval"))
        if payoff is not None and payoff.max_loss is None:
            issues.append(ValidationIssue("legs", "unbounded-loss strategies cannot be approved"))
        elif payoff is not None and plan.risk_budget is not None and payoff.max_loss > plan.risk_budget:
            issues.append(ValidationIssue("risk_budget", "maximum loss exceeds the plan risk budget"))
    if plan.catalyst_date is None:
        issues.append(ValidationIssue("catalyst_date", "no earnings/catalyst date recorded", "warning"))
    return tuple(issues)


def transition(plan: TradePlan, target: PlanStatus) -> TradePlan:
    allowed = {
        PlanStatus.DRAFT: {PlanStatus.APPROVED, PlanStatus.CANCELLED},
        PlanStatus.APPROVED: {PlanStatus.ENTERED, PlanStatus.CANCELLED},
        PlanStatus.ENTERED: {PlanStatus.MANAGED, PlanStatus.CLOSED},
        PlanStatus.MANAGED: {PlanStatus.CLOSED},
        PlanStatus.CLOSED: set(), PlanStatus.CANCELLED: set(),
    }
    if target not in allowed[plan.status]:
        raise ValueError(f"cannot transition {plan.status.value} -> {target.value}")
    if target is PlanStatus.APPROVED:
        errors = [i for i in validate_plan(plan, for_approval=True) if i.severity == "error"]
        if errors:
            raise ValueError("cannot approve plan: " + "; ".join(i.message for i in errors))
    return replace(plan, status=target, updated_at=datetime.now(timezone.utc))


class TradePlanStore:
    """Small JSON repository with atomic replacement and revision timestamps."""

    def __init__(self, path: Path = PLAN_STORE) -> None:
        self.path = path

    def list(self) -> list[TradePlan]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [_plan_from_dict(item) for item in raw]

    def get(self, plan_id: str) -> TradePlan | None:
        return next((plan for plan in self.list() if plan.plan_id == plan_id), None)

    def save(self, plan: TradePlan) -> TradePlan:
        plans = self.list()
        now = datetime.now(timezone.utc)
        plan = replace(plan, updated_at=now)
        replaced = False
        for idx, existing in enumerate(plans):
            if existing.plan_id == plan.plan_id:
                plans[idx] = plan
                replaced = True
                break
        if not replaced:
            plans.append(plan)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps([_plan_to_dict(p) for p in plans], indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(self.path)
        return plan


def _plan_to_dict(plan: TradePlan) -> dict:
    return {
        "plan_id": plan.plan_id, "ticker": plan.ticker, "strategy": plan.strategy,
        "thesis": plan.thesis, "bias": plan.bias, "entry_trigger": plan.entry_trigger,
        "invalidation": plan.invalidation, "exit_rule": plan.exit_rule,
        "risk_budget": str(plan.risk_budget) if plan.risk_budget is not None else None,
        "catalyst_date": plan.catalyst_date.isoformat() if plan.catalyst_date else None,
        "snapshot_id": plan.snapshot_id, "status": plan.status.value, "lesson": plan.lesson,
        "created_at": plan.created_at.isoformat(), "updated_at": plan.updated_at.isoformat(),
        "legs": [{"right": leg.right, "side": leg.side, "strike": str(leg.strike), "premium": str(leg.premium),
                  "quantity": str(leg.quantity), "multiplier": str(leg.multiplier),
                  "expiry": leg.expiry.isoformat() if leg.expiry else None} for leg in plan.legs],
    }


def _plan_from_dict(raw: dict) -> TradePlan:
    legs = tuple(OptionLeg(**{**leg, "expiry": date.fromisoformat(leg["expiry"]) if leg.get("expiry") else None}) for leg in raw["legs"])
    return TradePlan(
        ticker=raw["ticker"], strategy=raw["strategy"], legs=legs, thesis=raw.get("thesis", ""), bias=raw.get("bias", "neutral"),
        entry_trigger=raw.get("entry_trigger", ""), invalidation=raw.get("invalidation", ""), exit_rule=raw.get("exit_rule", ""),
        risk_budget=raw.get("risk_budget"), catalyst_date=date.fromisoformat(raw["catalyst_date"]) if raw.get("catalyst_date") else None,
        snapshot_id=raw.get("snapshot_id", ""), status=PlanStatus(raw.get("status", "draft")), lesson=raw.get("lesson", ""),
        plan_id=raw["plan_id"], created_at=datetime.fromisoformat(raw["created_at"]), updated_at=datetime.fromisoformat(raw["updated_at"]),
    )


def _parse_leg(value: str, expiry: date) -> OptionLeg:
    """Parse ``buy:call:100:3[:quantity[:multiplier]]`` for the tiny CLI."""
    parts = value.split(":")
    if len(parts) not in (4, 5, 6):
        raise ValueError("leg must be side:right:strike:premium[:quantity[:multiplier]]")
    side, right, strike, premium = parts[:4]
    quantity = parts[4] if len(parts) >= 5 else "1"
    multiplier = parts[5] if len(parts) == 6 else "100"
    return OptionLeg(right, side, strike, premium, quantity, multiplier, expiry)


def main(argv: Sequence[str] | None = None) -> int:
    """Minimal local CLI for creating/listing plans; it never contacts a broker."""
    parser = argparse.ArgumentParser(description="Local, read-only option trade-plan store.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="save a draft plan")
    create.add_argument("--ticker", required=True)
    create.add_argument("--strategy", required=True)
    create.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    create.add_argument("--leg", action="append", required=True, help="buy:call:strike:premium[:qty[:multiplier]]")
    create.add_argument("--thesis", default="")
    create.add_argument("--entry-trigger", default="")
    create.add_argument("--invalidation", default="")
    create.add_argument("--exit-rule", default="")
    create.add_argument("--risk-budget")
    create.add_argument("--snapshot-id", default="")
    create.add_argument("--approve", action="store_true")
    create.add_argument("--store", default=str(PLAN_STORE), help="local JSON path")
    list_parser = sub.add_parser("list", help="list saved plans")
    list_parser.add_argument("--store", default=str(PLAN_STORE), help="local JSON path")
    args = parser.parse_args(argv)
    store = TradePlanStore(Path(args.store))
    if args.command == "list":
        for plan in store.list():
            print(f"{plan.plan_id} {plan.status.value} {plan.ticker} {plan.strategy}")
        return 0
    try:
        expiry = date.fromisoformat(args.expiry)
        plan = TradePlan(args.ticker, args.strategy, tuple(_parse_leg(value, expiry) for value in args.leg),
                         thesis=args.thesis, entry_trigger=args.entry_trigger, invalidation=args.invalidation,
                         exit_rule=args.exit_rule, risk_budget=args.risk_budget, snapshot_id=args.snapshot_id)
        if args.approve:
            plan = transition(plan, PlanStatus.APPROVED)
    except ValueError as exc:
        parser.error(str(exc))
    saved = store.save(plan)
    print(f"Saved {saved.status.value} plan {saved.plan_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Next-week expiry trade risk engine (Google Doc spec §4).

Monitors open option contracts with 1–14 days to expiry and generates
actionable playbook signals. Builds on the existing ``alerting/expiry.py``
which finds expiring contracts — this module adds the *what to do* layer.

Signal types:
- CLOSE_POSITION — earnings play, capture IV crush at opening bell
- ROLL_SPREAD — earnings play violated by overnight gap, roll out 30–45 days
- CUT_TRADE — thesis invalidated within the expiry window
- ROLL_TIMELINE — macro intact but time running out, roll to next monthly
- NEEDS_REVIEW — can't determine from sheet data, needs manual technical check

Design: signals are based on what the sheet data can determine (DTE, earnings
proximity, P/L direction). Live technical analysis would require price feeds
the pipeline doesn't currently have, so those cases get NEEDS_REVIEW.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from analytics.earnings import is_near_earnings

log = logging.getLogger(__name__)

ZERO = Decimal("0")


class Signal(str, Enum):
    """Playbook signal for an expiring option position."""
    CLOSE_POSITION = "CLOSE POSITION"
    ROLL_SPREAD = "ROLL SPREAD"
    CUT_TRADE = "CUT TRADE"
    ROLL_TIMELINE = "ROLL TIMELINE"
    NEEDS_REVIEW = "NEEDS REVIEW"


@dataclass
class RiskAlert:
    """One actionable alert for an expiring option contract."""
    broker: str
    underlying: str
    option_type: str
    strike: str
    expiry: date
    days_to_expiry: int
    net_qty: Decimal
    signal: Signal
    reason: str
    tag: str = ""  # from the tagger, if available

    @property
    def side(self) -> str:
        return "long" if self.net_qty > 0 else "short"


def generate_risk_alerts(
    open_options: list[dict[str, Any]],
    tags: dict[str, str] | None = None,
    *,
    today: date | None = None,
    min_dte: int = 1,
    max_dte: int = 14,
) -> list[RiskAlert]:
    """Generate playbook signals for open options in the DTE window.

    ``open_options`` is from ``PortfolioWriter.read_all_option_trades()``,
    filtered to Status == "Open". ``tags`` maps dedup_key -> tag string.
    """
    today = today or date.today()
    tags = tags or {}
    alerts: list[RiskAlert] = []

    # Net positions by contract (same logic as expiry.py)
    _ACQUIRE_ACTIONS = {"Buy", "Opening Balance"}
    net_positions: dict[tuple, dict] = {}

    for td in open_options:
        trade = td["trade"]
        if td["status"] != "Open":
            continue

        dte = (trade.expiry - today).days
        if dte < min_dte or dte > max_dte:
            continue

        contract_key = (
            trade.broker.value,
            trade.underlying,
            trade.option_type.value,
            str(trade.strike),
            trade.expiry,
        )

        if contract_key not in net_positions:
            net_positions[contract_key] = {
                "broker": trade.broker.value,
                "underlying": trade.underlying,
                "option_type": trade.option_type.value,
                "strike": str(trade.strike),
                "expiry": trade.expiry,
                "dte": dte,
                "net_qty": ZERO,
                "tag": tags.get(trade.dedup_key, ""),
                "has_loss": False,
            }

        pos = net_positions[contract_key]
        signed_qty = trade.qty if trade.action.value in _ACQUIRE_ACTIONS else -trade.qty
        pos["net_qty"] += Decimal(str(signed_qty))

        # Check if this position is currently at a loss
        pl = _extract_pl(td)
        if pl is not None and pl < ZERO:
            pos["has_loss"] = True

        # Propagate tag if available
        tag = tags.get(trade.dedup_key, "")
        if tag and not pos["tag"]:
            pos["tag"] = tag

    # Generate signals for each net position
    for _key, pos in net_positions.items():
        if pos["net_qty"] == ZERO:
            continue  # fully closed

        signal, reason = _determine_signal(pos, today)
        alerts.append(RiskAlert(
            broker=pos["broker"],
            underlying=pos["underlying"],
            option_type=pos["option_type"],
            strike=pos["strike"],
            expiry=pos["expiry"],
            days_to_expiry=pos["dte"],
            net_qty=pos["net_qty"],
            signal=signal,
            reason=reason,
            tag=pos["tag"],
        ))

    alerts.sort(key=lambda a: (a.days_to_expiry, a.underlying, a.strike))
    return alerts


def _determine_signal(pos: dict, today: date) -> tuple[Signal, str]:
    """Pick the right playbook signal based on available data."""
    dte = pos["dte"]
    tag = pos["tag"]
    underlying = pos["underlying"]
    has_loss = pos["has_loss"]

    # Earnings plays
    is_earnings = (
        "iv crush" in tag.lower() or
        "earnings" in tag.lower() or
        is_near_earnings(underlying, today, window_days=3)
    )

    if is_earnings:
        if dte <= 2:
            return (
                Signal.CLOSE_POSITION,
                f"Earnings play with {dte}d to expiry — close at opening bell "
                f"to capture IV crush. Don't hold through expiry."
            )
        if has_loss:
            return (
                Signal.ROLL_SPREAD,
                f"Earnings play showing loss with {dte}d left — consider "
                f"rolling untested side closer to the money, or rolling "
                f"entire structure out 30–45 days to recover."
            )
        return (
            Signal.CLOSE_POSITION,
            f"Earnings play with {dte}d to expiry — plan to close in "
            f"the first 15 minutes post-earnings bell for peak IV collapse."
        )

    # Non-earnings: day/medium-term plays
    if has_loss and dte <= 5:
        return (
            Signal.CUT_TRADE,
            f"Position at a loss with only {dte}d to expiry — thesis "
            f"may be invalidated. Cut losses or roll if conviction remains."
        )

    if dte <= 3:
        return (
            Signal.ROLL_TIMELINE,
            f"Only {dte}d to expiry — if macro trend is intact, roll to "
            f"next monthly expiration at delta 0.30–0.40."
        )

    if has_loss:
        return (
            Signal.NEEDS_REVIEW,
            f"Position at a loss with {dte}d left — check if primary thesis "
            f"is still intact and daily technical levels hold."
        )

    return (
        Signal.NEEDS_REVIEW,
        f"{dte}d to expiry — review position against current technicals "
        f"and decide whether to hold, roll, or close."
    )


def _extract_pl(trade_dict: dict[str, Any]) -> Decimal | None:
    """Best-effort P/L extraction from a trade dict."""
    raw = trade_dict.get("raw", [])
    # Options P/L is typically at index 14 ("P/L") or 15 ("P/L (SGD)")
    for idx in (14, 15):
        if idx < len(raw):
            val = raw[idx]
            if val != "" and val is not None:
                try:
                    return Decimal(str(val).replace(",", ""))
                except Exception:
                    pass
    return None


def format_risk_alert_message(
    alerts: list[RiskAlert],
    *,
    today: date | None = None,
) -> str:
    """Format risk alerts as a Telegram-ready message."""
    today = today or date.today()

    if not alerts:
        return f"✅ No open options in the 1–14 day expiry window (as of {today:%d %b %Y})."

    lines = [
        f"🚨 Option Risk Alerts — {len(alerts)} position(s) expiring within 14 days "
        f"(as of {today:%d %b %Y}):",
        "",
    ]

    for a in alerts:
        emoji = {
            Signal.CLOSE_POSITION: "🔴",
            Signal.ROLL_SPREAD: "🟠",
            Signal.CUT_TRADE: "🔴",
            Signal.ROLL_TIMELINE: "🟡",
            Signal.NEEDS_REVIEW: "⚪",
        }.get(a.signal, "⚪")

        strike_display = a.strike.lstrip("$").strip()
        lines.append(
            f"{emoji} [{a.signal.value}] {a.underlying} {strike_display} "
            f"{a.option_type} · {a.side} ×{abs(a.net_qty):.0f} · "
            f"{a.days_to_expiry}d left"
        )
        lines.append(f"   {a.reason}")
        lines.append("")

    return "\n".join(lines)

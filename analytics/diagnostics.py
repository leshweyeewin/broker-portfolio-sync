"""Analytical diagnostic calculators (Google Doc spec §3).

Three diagnostics that process tagged trades to identify structural
performance leaks:

1. **Earnings IV Crush Analyzer** — avg win vs avg loss; warns on asymmetric
   downside (a few big losses wiping out consistent small wins).
2. **Intraday Fee Drag Calculator** — for day trades, checks if fees eat
   >15% of gross profits.
3. **Medium-Term Performance Metric** — rolling return vs SPY benchmark to
   measure relative alpha.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from analytics.tagger import TAG_DAY_TRADE, TAG_EARNINGS_IV_CRUSH, TAG_MEDIUM_TERM

ZERO = Decimal("0")
FEE_DRAG_THRESHOLD = Decimal("0.15")  # 15%
LOSS_ASYMMETRY_RATIO = Decimal("2")   # avg loss > 2× avg win = warning


@dataclass
class IVCrushResult:
    """Result of the earnings IV crush analysis."""
    win_count: int = 0
    loss_count: int = 0
    avg_win: Decimal = ZERO
    avg_loss: Decimal = ZERO
    total_pl: Decimal = ZERO
    risk_warning: bool = False
    warning_message: str = ""


@dataclass
class FeeDragResult:
    """Result of the intraday fee drag analysis."""
    total_fees: Decimal = ZERO
    gross_profit: Decimal = ZERO
    fee_drag_pct: Decimal = ZERO
    trade_count: int = 0
    alert: bool = False
    alert_message: str = ""


@dataclass
class MediumTermResult:
    """Result of medium-term performance analysis."""
    total_return_pct: Decimal = ZERO
    trade_count: int = 0
    total_pl_sgd: Decimal = ZERO
    # SPY benchmark comparison (filled when available)
    spy_return_pct: Decimal | None = None
    alpha_pct: Decimal | None = None


def _get_pl(trade_dict: dict[str, Any]) -> Decimal | None:
    """Extract realized P/L from a trade dict's raw row values."""
    trade = trade_dict["trade"]
    # For stocks: realized P/L (SGD) is in the raw row
    # For options: P/L (SGD) is in the raw row
    raw = trade_dict.get("raw", [])

    # Try to get the SGD P/L column (varies by tab)
    # Stocks: col index 11 = "Realized P/L (SGD)"
    # Options: col index 15 = "P/L (SGD)"
    for i in range(len(raw) - 1, -1, -1):
        val = raw[i] if i < len(raw) else ""
        if val == "" or val is None:
            continue
        # Check for dedup_key pattern to skip it
        if isinstance(val, str) and (":" in val and len(val) > 10):
            continue
        try:
            return Decimal(str(val).replace(",", ""))
        except Exception:
            continue
    return None


def _get_fee(trade_dict: dict[str, Any]) -> Decimal:
    """Extract fee from a trade dict."""
    trade = trade_dict["trade"]
    try:
        return Decimal(str(trade.fee))
    except Exception:
        return ZERO


def _get_pl_from_trade(trade_dict: dict[str, Any], pl_col_name: str, headers: list[str]) -> Decimal | None:
    """Get P/L value using column header name."""
    raw = trade_dict.get("raw", [])
    if pl_col_name not in headers:
        return None
    idx = headers.index(pl_col_name)
    if idx >= len(raw):
        return None
    val = raw[idx]
    if val == "" or val is None:
        return None
    try:
        return Decimal(str(val).replace(",", ""))
    except Exception:
        return None


def earnings_iv_crush_analysis(
    tagged_trades: list[tuple[dict[str, Any], str]],
    pl_col: str = "Realized P/L (SGD)",
    headers: list[str] | None = None,
) -> IVCrushResult:
    """Analyze earnings IV crush trades for win/loss asymmetry.

    ``tagged_trades`` is a list of (trade_dict, tag) tuples.
    """
    result = IVCrushResult()
    wins: list[Decimal] = []
    losses: list[Decimal] = []

    for trade_dict, tag in tagged_trades:
        if tag != TAG_EARNINGS_IV_CRUSH:
            continue
        if trade_dict["status"] != "Closed":
            continue

        pl: Decimal | None = None
        if headers:
            pl = _get_pl_from_trade(trade_dict, pl_col, headers)
        if pl is None:
            pl = _get_pl(trade_dict)
        if pl is None:
            continue

        result.total_pl += pl
        if pl > ZERO:
            wins.append(pl)
        elif pl < ZERO:
            losses.append(pl)

    result.win_count = len(wins)
    result.loss_count = len(losses)
    result.avg_win = sum(wins) / len(wins) if wins else ZERO
    result.avg_loss = abs(sum(losses) / len(losses)) if losses else ZERO

    if result.avg_win > ZERO and result.avg_loss > result.avg_win * LOSS_ASYMMETRY_RATIO:
        result.risk_warning = True
        result.warning_message = (
            f"⚠️ Asymmetric downside detected: avg loss ${result.avg_loss:.2f} "
            f"is >{LOSS_ASYMMETRY_RATIO}× avg win ${result.avg_win:.2f}. "
            f"A few large losses are wiping out consistent wins."
        )

    return result


def intraday_fee_drag(
    tagged_trades: list[tuple[dict[str, Any], str]],
) -> FeeDragResult:
    """Calculate fee drag on day trades.

    Triggers an alert if fees exceed 15% of gross profits.
    """
    result = FeeDragResult()

    for trade_dict, tag in tagged_trades:
        if tag != TAG_DAY_TRADE:
            continue
        result.trade_count += 1

        fee = _get_fee(trade_dict)
        result.total_fees += abs(fee)

        pl = _get_pl(trade_dict)
        if pl is not None and pl > ZERO:
            result.gross_profit += pl

    if result.gross_profit > ZERO:
        result.fee_drag_pct = result.total_fees / result.gross_profit
    elif result.total_fees > ZERO:
        result.fee_drag_pct = Decimal("1")  # 100% — all fees, no profit

    if result.fee_drag_pct > FEE_DRAG_THRESHOLD:
        result.alert = True
        result.alert_message = (
            f"🚨 High Fee Drag Detected: fees are {result.fee_drag_pct:.1%} of "
            f"gross day-trade profits (${result.total_fees:.2f} fees vs "
            f"${result.gross_profit:.2f} profit). Review broker pricing model "
            f"or increase minimum profit targets."
        )

    return result


def medium_term_performance(
    tagged_trades: list[tuple[dict[str, Any], str]],
    total_capital_sgd: Decimal | None = None,
) -> MediumTermResult:
    """Calculate medium-term trade performance.

    ``total_capital_sgd`` is the total capital deployed (for % return calc).
    SPY benchmark comparison is deferred — alpha is None until a benchmark
    source is integrated.
    """
    result = MediumTermResult()

    for trade_dict, tag in tagged_trades:
        if tag != TAG_MEDIUM_TERM:
            continue
        if trade_dict["status"] != "Closed":
            continue
        result.trade_count += 1

        pl = _get_pl(trade_dict)
        if pl is not None:
            result.total_pl_sgd += pl

    if total_capital_sgd and total_capital_sgd > ZERO:
        result.total_return_pct = (result.total_pl_sgd / total_capital_sgd) * 100

    return result

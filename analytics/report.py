"""Analytics report orchestrator — reads sheet, runs all analytics, sends report.

This is the top-level entry point for the analytics module. It:
1. Reads Stocks and Options data from the Google Sheet
2. Tags trades via the tagger
3. Runs all three diagnostic calculators
4. Generates risk alerts for expiring options
5. Formats everything into a Telegram report
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Optional

from analytics.diagnostics import (
    IVCrushResult,
    FeeDragResult,
    MediumTermResult,
    earnings_iv_crush_analysis,
    intraday_fee_drag,
    medium_term_performance,
)
from analytics.risk_engine import (
    RiskAlert,
    format_risk_alert_message,
    generate_risk_alerts,
)
from analytics.tagger import (
    tag_option_trades,
    tag_stock_trades,
)

log = logging.getLogger(__name__)

ZERO = Decimal("0")


@dataclass
class AnalyticsReport:
    """Full analytics report — all diagnostic results in one place."""
    iv_crush: IVCrushResult = field(default_factory=IVCrushResult)
    fee_drag: FeeDragResult = field(default_factory=FeeDragResult)
    medium_term: MediumTermResult = field(default_factory=MediumTermResult)
    risk_alerts: list[RiskAlert] = field(default_factory=list)
    stock_tag_counts: dict[str, int] = field(default_factory=dict)
    option_tag_counts: dict[str, int] = field(default_factory=dict)


def run_analytics(
    writer,
    *,
    today: date | None = None,
    total_capital_sgd: Decimal | None = None,
) -> AnalyticsReport:
    """Run the full analytics pipeline against the Google Sheet.

    ``writer`` is a ``PortfolioWriter`` instance (or fake in tests).
    """
    today = today or date.today()
    report = AnalyticsReport()

    # 1. Read all trades from the sheet
    stock_trades = writer.read_all_stock_trades()
    option_trades = writer.read_all_option_trades()

    # 2. Tag trades
    stock_tags = tag_stock_trades(stock_trades)
    option_tags = tag_option_trades(option_trades)

    # Count tags
    for tag in stock_tags.values():
        label = tag or "Untagged"
        report.stock_tag_counts[label] = report.stock_tag_counts.get(label, 0) + 1
    for tag in option_tags.values():
        label = tag or "Untagged"
        report.option_tag_counts[label] = report.option_tag_counts.get(label, 0) + 1

    # 3. Build tagged lists for diagnostics
    stock_tagged = [(td, stock_tags.get(td["trade"].dedup_key, "")) for td in stock_trades]
    option_tagged = [(td, option_tags.get(td["trade"].dedup_key, "")) for td in option_trades]
    all_tagged = stock_tagged + option_tagged

    # 4. Run diagnostics
    report.iv_crush = earnings_iv_crush_analysis(all_tagged)
    report.fee_drag = intraday_fee_drag(all_tagged)
    report.medium_term = medium_term_performance(all_tagged, total_capital_sgd)

    # 5. Generate risk alerts
    report.risk_alerts = generate_risk_alerts(
        option_trades, option_tags, today=today
    )

    return report


def format_telegram_report(report: AnalyticsReport, *, today: date | None = None) -> str:
    """Format the full analytics report as a Telegram message."""
    today = today or date.today()
    sections: list[str] = []

    # Header
    sections.append(f"📊 Analytics Report — {today:%d %b %Y}")
    sections.append("")

    # Tag summary
    all_tags = {}
    for label, count in {**report.stock_tag_counts, **report.option_tag_counts}.items():
        all_tags[label] = all_tags.get(label, 0) + count
    if all_tags:
        sections.append("🏷️ Strategy Tags:")
        for label, count in sorted(all_tags.items()):
            sections.append(f"   • {label}: {count}")
        sections.append("")

    # IV Crush analysis
    ic = report.iv_crush
    if ic.win_count + ic.loss_count > 0:
        sections.append("📈 Earnings IV Crush Performance:")
        sections.append(
            f"   Wins: {ic.win_count} (avg ${ic.avg_win:.2f}) · "
            f"Losses: {ic.loss_count} (avg ${ic.avg_loss:.2f})"
        )
        sections.append(f"   Net P/L: ${ic.total_pl:.2f}")
        if ic.risk_warning:
            sections.append(f"   {ic.warning_message}")
        sections.append("")

    # Fee drag
    fd = report.fee_drag
    if fd.trade_count > 0:
        sections.append(f"💸 Day Trade Fee Drag ({fd.trade_count} trades):")
        sections.append(
            f"   Fees: ${fd.total_fees:.2f} · Gross Profit: ${fd.gross_profit:.2f} · "
            f"Drag: {fd.fee_drag_pct:.1%}"
        )
        if fd.alert:
            sections.append(f"   {fd.alert_message}")
        sections.append("")

    # Medium-term
    mt = report.medium_term
    if mt.trade_count > 0:
        sections.append(f"📊 Medium-Term Trades ({mt.trade_count} closed):")
        sections.append(f"   Total P/L (SGD): ${mt.total_pl_sgd:.2f}")
        if mt.total_return_pct:
            sections.append(f"   Return: {mt.total_return_pct:.2f}%")
        sections.append("")

    # Risk alerts
    if report.risk_alerts:
        sections.append(format_risk_alert_message(report.risk_alerts, today=today))

    return "\n".join(sections) if sections else "📊 No analytics data available."


def main(argv=None) -> int:
    """CLI entry point for running analytics standalone."""
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run portfolio analytics")
    parser.add_argument("--notify", action="store_true", help="Send report via Telegram")
    args = parser.parse_args(argv)

    from config.settings import get_service_account_info, get_spreadsheet_id
    from sheets.writer import PortfolioWriter, SheetClient

    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)

    report = run_analytics(writer)
    message = format_telegram_report(report)

    print(message)

    if args.notify:
        from alerting.notify import notify_safe
        delivered = notify_safe(message)
        log.info("Analytics report delivered: %s", delivered)
        return 0 if delivered else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

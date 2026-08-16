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

from analytics.market_scan import (
    TickerMover,
    UpcomingEarnings,
    get_daily_movers,
    get_upcoming_earnings,
    scan_short_option_picks,
)
from analytics.screener import ScreenerResult
from analytics.swing import SwingSetup, scan_swing_setups, format_swing_message
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
    """Full analytics report — diagnostics, market scans, and risk alerts."""
    iv_crush: IVCrushResult = field(default_factory=IVCrushResult)
    fee_drag: FeeDragResult = field(default_factory=FeeDragResult)
    medium_term: MediumTermResult = field(default_factory=MediumTermResult)
    risk_alerts: list[RiskAlert] = field(default_factory=list)
    stock_tag_counts: dict[str, int] = field(default_factory=dict)
    option_tag_counts: dict[str, int] = field(default_factory=dict)
    bullish_movers: list[TickerMover] = field(default_factory=list)
    bearish_movers: list[TickerMover] = field(default_factory=list)
    upcoming_earnings: list[UpcomingEarnings] = field(default_factory=list)
    screener_picks: list[ScreenerResult] = field(default_factory=list)
    swing_setups: list[SwingSetup] = field(default_factory=list)


def run_analytics(
    writer,
    *,
    today: date | None = None,
    total_capital_sgd: Decimal | None = None,
    scan_market: bool = True,
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

    # 5. Generate risk alerts for expiring options (1–14 DTE)
    report.risk_alerts = generate_risk_alerts(
        option_trades, option_tags, today=today
    )

    # 6. Market scanning (daily movers, upcoming earnings, short put/call picks)
    if scan_market:
        # Collect universe of actively traded tickers
        tickers = set()
        for td in stock_trades:
            tickers.add(td["trade"].ticker)
        for td in option_trades:
            tickers.add(td["trade"].underlying)
        ticker_list = sorted(t for t in tickers if t and len(t) <= 6)

        # Combine sheet tickers + all monitored watchlist tickers from earnings cache
        from analytics.earnings import _load_static_cache
        watchlist_tickers = list(_load_static_cache().keys())
        full_universe = sorted(set(ticker_list + watchlist_tickers))

        if ticker_list:
            try:
                bullish, bearish = get_daily_movers(ticker_list)
                report.bullish_movers = bullish
                report.bearish_movers = bearish
            except Exception as exc:
                log.debug("Daily movers scan error: %s", exc)

            try:
                report.upcoming_earnings = get_upcoming_earnings(full_universe, today=today, days_ahead=7)
            except Exception as exc:
                log.debug("Earnings calendar scan error: %s", exc)

            try:
                # Watchlist-first income scan (held + monitored names), not just
                # traded tickers; benchmarks stay off by default.
                report.screener_picks = scan_short_option_picks(full_universe, today=today)
            except Exception as exc:
                log.debug("Option screener scan error: %s", exc)

            try:
                report.swing_setups = scan_swing_setups(full_universe, today=today)
            except Exception as exc:
                log.debug("Swing setup scan error: %s", exc)

    return report


def format_telegram_report(report: AnalyticsReport, *, today: date | None = None) -> str:
    """Format the full analytics report as a Telegram message."""
    today = today or date.today()
    sections: list[str] = []

    # Header
    sections.append(f"📊 Daily Market & Portfolio Report — {today:%d %b %Y}")
    sections.append("")

    # 1. Daily Bullish / Bearish Movers
    if report.bullish_movers or report.bearish_movers:
        sections.append("🚀 Daily Ticker Movers:")
        if report.bullish_movers:
            top_bulls = ", ".join(f"🟢 {m.ticker} +{m.change_pct:.1f}% (${m.price})" for m in report.bullish_movers[:4])
            sections.append(f"   Bullish: {top_bulls}")
        if report.bearish_movers:
            top_bears = ", ".join(f"🔴 {m.ticker} {m.change_pct:.1f}% (${m.price})" for m in report.bearish_movers[:4])
            sections.append(f"   Bearish: {top_bears}")
        sections.append("")

    # 2. Upcoming Earnings (Next 1–2 Days)
    if report.upcoming_earnings:
        sections.append("📅 Upcoming Earnings (Prepare IV Crush Plays):")
        for e in report.upcoming_earnings:
            sections.append(f"   ⚡ {e.ticker} · {e.note} ({e.earnings_date:%a %d %b})")
        sections.append("")

    # 3. High-Probability Short Put / Short Call Picks (Systematic Screener)
    if report.screener_picks:
        puts = [p for p in report.screener_picks if p.option_type == "Put"]
        calls = [p for p in report.screener_picks if p.option_type == "Call"]

        sections.append("🔎 Systematic Short Option Picks (Δ 0.10–0.15, OI>500):")
        if puts:
            sections.append("   🟢 Bullish Income (Short Put):")
            for p in puts[:3]:
                sections.append(
                    f"      • {p.symbol} ${p.strike} Put exp {p.expiry} · "
                    f"Δ {p.delta:.2f} · Mid ${p.mid_price:.2f} · OI {p.open_interest:,}"
                )
        if calls:
            sections.append("   🔴 Bearish Income (Short Call):")
            for p in calls[:3]:
                sections.append(
                    f"      • {p.symbol} ${p.strike} Call exp {p.expiry} · "
                    f"Δ {p.delta:.2f} · Mid ${p.mid_price:.2f} · OI {p.open_interest:,}"
                )
        sections.append("")

    # 3b. Swing Setups (technical entries — Breakout / Pullback-buy)
    swing_msg = format_swing_message(report.swing_setups)
    if swing_msg:
        sections.append(swing_msg)
        sections.append("")

    # 4. Risk Alerts (Expiring Options)
    if report.risk_alerts:
        sections.append(format_risk_alert_message(report.risk_alerts, today=today))
        sections.append("")

    # 5. Diagnostic Summary (Fee drag & IV Crush)
    fd = report.fee_drag
    if fd.alert:
        sections.append(f"💸 {fd.alert_message}")
        sections.append("")
    ic = report.iv_crush
    if ic.risk_warning:
        sections.append(f"📈 {ic.warning_message}")
        sections.append("")

    return "\n".join(sections).strip() if sections else "📊 No analytics data available."



def main(argv=None) -> int:
    """CLI entry point for running analytics standalone."""
    import argparse
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
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

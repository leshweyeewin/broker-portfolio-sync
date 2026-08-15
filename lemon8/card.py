"""Lemon8 image card generator (step 10 — BUILD_SPEC.md §11b).

Generates text/SVG/HTML card preview layouts for social image posts.

Privacy Constraint (Load-bearing):
- Default ``show_dollar_amounts=False`` renders ONLY return %, ticker, date, and asset type.
- Absolute dollar amounts are hidden unless ``show_dollar_amounts=True`` is explicitly set.
"""

from __future__ import annotations

from typing import Optional
from lemon8.reader import ClosedPosition


def render_card_summary(
    pos: ClosedPosition,
    *,
    show_dollar_amounts: bool = False,
) -> str:
    """Render a text summary card block for social post images or previews."""
    return_str = (
        f"{pos.return_pct:+.1f}%"
        if pos.return_pct is not None
        else "Closed"
    )
    win_indicator = "🚀 WIN" if (pos.is_win is True) else ("📉 LOSS" if (pos.is_win is False) else "📊 TRADE")

    lines = [
        "┌──────────────────────────────────────────┐",
        f"│  {win_indicator:<12} {pos.label:>24}  │",
        f"│  Return: {return_str:<30}  │",
        f"│  Date: {pos.close_date:<32}  │",
    ]

    # Options carry more useful context than stocks — surface the strategy and
    # expiry so a screenshot card stands on its own.
    if pos.asset == "option":
        if pos.strategy:
            lines.append(f"│  Strategy: {pos.strategy:<28}  │")
        if pos.expiry:
            lines.append(f"│  Expiry: {pos.expiry:<30}  │")

    if show_dollar_amounts and pos.realized_pl is not None:
        pl_str = f"{pos.realized_pl:+.2f} {pos.currency}"
        lines.append(f"│  P/L: {pl_str:<33}  │")

    lines.append("└──────────────────────────────────────────┘")
    return "\n".join(lines)


def render_card_svg(
    pos: ClosedPosition,
    *,
    show_dollar_amounts: bool = False,
) -> str:
    """Render a clean SVG card markup for Lemon8 cover image generation.

    One card = one screenshot of a single trade. The weekly journal renders one
    of these per closed position, so a post carries a carousel of cards + a
    single caption.
    """
    return_str = (
        f"{pos.return_pct:+.1f}%"
        if pos.return_pct is not None
        else "CLOSED"
    )
    bg_color = "#10B981" if (pos.is_win is True) else ("#EF4444" if (pos.is_win is False) else "#6B7280")

    # For options the strategy (e.g. "Short Put") is the most descriptive
    # subtitle; stocks just say STOCK.
    subtitle = f"{(pos.strategy or pos.asset).upper()} • Closed {pos.close_date}"

    # Extra detail lines stack under the return figure. Options add an expiry;
    # dollars remain an explicit opt-in (privacy rule).
    detail_lines = []
    y = 250
    if pos.asset == "option" and pos.expiry:
        detail_lines.append(
            f'<text x="40" y="{y}" font-family="sans-serif" font-size="22" fill="#9CA3AF">Expires {pos.expiry}</text>'
        )
        y += 40
    if show_dollar_amounts and pos.realized_pl is not None:
        detail_lines.append(
            f'<text x="40" y="{y}" font-family="sans-serif" font-size="24" fill="#F3F4F6">P/L: {pos.realized_pl:+.2f} {pos.currency}</text>'
        )
    details = "\n  ".join(detail_lines)

    svg = f"""<svg width="600" height="400" xmlns="http://www.w3.org/2000/svg">
  <rect width="600" height="400" rx="24" fill="#1F2937"/>
  <rect x="0" y="0" width="600" height="16" fill="{bg_color}"/>
  <text x="40" y="80" font-family="sans-serif" font-weight="bold" font-size="36" fill="#FFFFFF">{pos.label}</text>
  <text x="40" y="130" font-family="sans-serif" font-size="20" fill="#9CA3AF">{subtitle}</text>
  <text x="40" y="190" font-family="sans-serif" font-weight="bold" font-size="48" fill="{bg_color}">{return_str}</text>
  {details}
</svg>"""
    return svg

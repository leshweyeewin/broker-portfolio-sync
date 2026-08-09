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
    """Render a clean SVG card markup for Lemon8 cover image generation."""
    return_str = (
        f"{pos.return_pct:+.1f}%"
        if pos.return_pct is not None
        else "CLOSED"
    )
    bg_color = "#10B981" if (pos.is_win is True) else ("#EF4444" if (pos.is_win is False) else "#6B7280")
    
    dollar_line = ""
    if show_dollar_amounts and pos.realized_pl is not None:
        dollar_line = f'<text x="40" y="240" font-family="sans-serif" font-size="24" fill="#F3F4F6">P/L: {pos.realized_pl:+.2f} {pos.currency}</text>'

    svg = f"""<svg width="600" height="400" xmlns="http://www.w3.org/2000/svg">
  <rect width="600" height="400" rx="24" fill="#1F2937"/>
  <rect x="0" y="0" width="600" height="16" fill="{bg_color}"/>
  <text x="40" y="80" font-family="sans-serif" font-weight="bold" font-size="36" fill="#FFFFFF">{pos.label}</text>
  <text x="40" y="130" font-family="sans-serif" font-size="20" fill="#9CA3AF">{pos.asset.upper()} • Closed {pos.close_date}</text>
  <text x="40" y="190" font-family="sans-serif" font-weight="bold" font-size="48" fill="{bg_color}">{return_str}</text>
  {dollar_line}
</svg>"""
    return svg

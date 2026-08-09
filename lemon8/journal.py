"""Lemon8 journal post generator (step 10 — BUILD_SPEC.md §11b).

Generates copy-paste teaser packages for social media (Lemon8, TikTok) and blog
drafts from closed trade positions read from Google Sheets.

LOAD-BEARING PRIVACY PRINCIPLE:
- Default ``show_dollar_amounts=False`` renders ONLY return % and trade thesis/reasoning.
- Absolute dollar amounts ($ / SGD / P/L) and portfolio size are NEVER shown by default.
- Showing dollars requires explicit opt-in per post (``show_dollar_amounts=True``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lemon8.reader import ClosedPosition
from lemon8.card import render_card_summary, render_card_svg


@dataclass(frozen=True)
class JournalPackage:
    """Complete ready-to-use journal output package."""

    position: ClosedPosition
    reasoning: str
    show_dollar_amounts: bool
    caption: str
    blog_draft: str
    card_summary: str
    card_svg: str


def format_caption(
    pos: ClosedPosition,
    reasoning: str = "",
    *,
    show_dollar_amounts: bool = False,
    blog_url: str = "",
) -> str:
    """Format a Lemon8 / TikTok caption teaser.

    Defaults to percentage-only view. No absolute dollar amounts are included
    unless ``show_dollar_amounts=True``.
    """
    win_emoji = "🚀" if (pos.is_win is True) else ("📉" if (pos.is_win is False) else "📊")
    return_str = (
        f"{pos.return_pct:+.1f}%"
        if pos.return_pct is not None
        else "Closed"
    )

    lines = [
        f"{win_emoji} Trade Journal: {pos.label} ({return_str})",
        "",
        f"Asset: {pos.asset.capitalize()}",
        f"Closed Date: {pos.close_date}",
        f"Result: {return_str}",
    ]

    if show_dollar_amounts and pos.realized_pl is not None:
        lines.append(f"Realized P/L: {pos.realized_pl:+.2f} {pos.currency}")

    if reasoning.strip():
        lines.extend([
            "",
            "💡 Trade Thesis & Key Takeaways:",
            reasoning.strip(),
        ])

    lines.extend([
        "",
        "👇 Full breakdown & reasoning on the blog:",
        blog_url if blog_url else "[Link in bio / Blog Draft]",
        "",
        f"#TradingJournal #{pos.symbol} #Investing #OptionsTrading #StockMarket",
    ])

    return "\n".join(lines)


def format_blog_draft(
    pos: ClosedPosition,
    reasoning: str = "",
    *,
    show_dollar_amounts: bool = False,
) -> str:
    """Format a canonical Markdown blog post draft.

    Blog drafts present percentages and reasoning by default.
    """
    return_str = (
        f"{pos.return_pct:+.1f}%"
        if pos.return_pct is not None
        else "N/A"
    )

    lines = [
        f"# Trade Retrospective: {pos.label}",
        "",
        f"**Date:** {pos.close_date}  ",
        f"**Asset Class:** {pos.asset.capitalize()}  ",
        f"**Return:** `{return_str}`  ",
    ]

    if show_dollar_amounts and pos.realized_pl is not None:
        lines.append(f"**Realized P/L:** `{pos.realized_pl:+.2f} {pos.currency}`  ")

    lines.extend([
        "",
        "## 1. Trade Rationale & Strategy",
        reasoning.strip() if reasoning.strip() else "_No notes recorded for this execution._",
        "",
        "## 2. Lessons & Retrospective",
        "- How did execution align with the original plan?",
        "- Were risk parameters respected?",
        "",
        "---",
        "_Generated automatically from portfolio sync logs._",
    ])

    return "\n".join(lines)


def generate_journal_package(
    pos: ClosedPosition,
    reasoning: str = "",
    *,
    show_dollar_amounts: bool = False,
    blog_url: str = "",
) -> JournalPackage:
    """Generate a complete journal package for a closed position."""
    caption = format_caption(
        pos, reasoning, show_dollar_amounts=show_dollar_amounts, blog_url=blog_url
    )
    blog_draft = format_blog_draft(
        pos, reasoning, show_dollar_amounts=show_dollar_amounts
    )
    card_summary = render_card_summary(
        pos, show_dollar_amounts=show_dollar_amounts
    )
    card_svg = render_card_svg(
        pos, show_dollar_amounts=show_dollar_amounts
    )

    return JournalPackage(
        position=pos,
        reasoning=reasoning,
        show_dollar_amounts=show_dollar_amounts,
        caption=caption,
        blog_draft=blog_draft,
        card_summary=card_summary,
        card_svg=card_svg,
    )

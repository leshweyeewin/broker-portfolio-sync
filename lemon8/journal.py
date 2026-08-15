"""Lemon8 weekly journal generator (step 10 — BUILD_SPEC.md §11b).

Turns the week's closed positions into ONE social post: a single caption + a
single long-form blog draft, plus one screenshot card per trade (the carousel
of images that post carries). This is deliberately *not* one post/blog per
transaction — a week's trades are a single journal entry.

LOAD-BEARING PRIVACY PRINCIPLE:
- Default ``show_dollar_amounts=False`` renders ONLY return % and trade thesis/reasoning.
- Absolute dollar amounts ($ / SGD / P/L) and portfolio size are NEVER shown by default.
- Showing dollars requires explicit opt-in (``show_dollar_amounts=True``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from lemon8.reader import ClosedPosition
from lemon8.card import render_card_svg


@dataclass(frozen=True)
class WeeklyJournal:
    """One week's journal deliverable: a single caption + blog + N card images."""

    week_ending: date
    positions: list[ClosedPosition]
    show_dollar_amounts: bool
    caption: str
    blog_draft: str
    cards: list[tuple[ClosedPosition, str]]  # (position, card_svg) — one screenshot each


# --------------------------------------------------------------------------- #
# Small shared formatters
# --------------------------------------------------------------------------- #

def _return_str(pos: ClosedPosition, *, dash: str = "Closed") -> str:
    return f"{pos.return_pct:+.1f}%" if pos.return_pct is not None else dash


def _win_emoji(pos: ClosedPosition) -> str:
    return "🚀" if (pos.is_win is True) else ("📉" if (pos.is_win is False) else "📊")


def _unique_symbols(positions: list[ClosedPosition]) -> list[str]:
    seen: dict[str, None] = {}
    for p in positions:
        seen.setdefault(p.symbol, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# One caption for the whole post
# --------------------------------------------------------------------------- #

def format_weekly_caption(
    positions: list[ClosedPosition],
    week_ending: date,
    *,
    show_dollar_amounts: bool = False,
    blog_url: str = "",
) -> str:
    """Format the single Lemon8 / TikTok caption for the week's post.

    Lists every closed trade with its return; the image carousel carries the
    per-trade cards. Percentage-only unless ``show_dollar_amounts=True``.
    """
    if not positions:
        return (
            f"📊 Weekly Trade Journal — week ending {week_ending:%d %b %Y}\n\n"
            "No trades closed this week. Staying patient. 🧘"
        )

    lines = [
        f"📊 Weekly Trade Journal — week ending {week_ending:%d %b %Y}",
        "",
        f"{len(positions)} trade(s) closed this week:",
    ]
    for pos in positions:
        line = f"{_win_emoji(pos)} {pos.label}: {_return_str(pos)}"
        if show_dollar_amounts and pos.realized_pl is not None:
            line += f" ({pos.realized_pl:+.2f} {pos.currency})"
        lines.append(line)

    tags = " ".join(f"#{s}" for s in _unique_symbols(positions))
    lines.extend([
        "",
        "👇 Full breakdown & reasoning on the blog:",
        blog_url if blog_url else "[Link in bio / Blog Draft]",
        "",
        f"#TradingJournal #Investing #OptionsTrading #StockMarket {tags}".rstrip(),
    ])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# One blog post for the whole week
# --------------------------------------------------------------------------- #

def format_weekly_blog(
    positions: list[ClosedPosition],
    week_ending: date,
    *,
    show_dollar_amounts: bool = False,
) -> str:
    """Format the single Markdown blog draft covering all of the week's trades.

    One section per trade (percentage + context); rationale is left as a TODO
    for the user to fill before publishing — the numbers are automatic, the
    story is not.
    """
    wins = sum(1 for p in positions if p.is_win is True)
    losses = sum(1 for p in positions if p.is_win is False)

    lines = [
        f"# Weekly Trading Journal — week ending {week_ending.isoformat()}",
        "",
    ]
    if positions:
        lines.append(
            f"**{len(positions)} position(s) closed** this week — "
            f"{wins} win(s), {losses} loss(es)."
        )
    else:
        lines.append("**No positions closed** this week.")
    lines.append("")

    for pos in positions:
        lines.extend(_blog_section(pos, show_dollar_amounts))

    lines.extend([
        "---",
        "_Generated automatically from portfolio sync logs._",
    ])
    return "\n".join(lines)


def _blog_section(pos: ClosedPosition, show_dollar_amounts: bool) -> list[str]:
    out = [
        f"## {_win_emoji(pos)} {pos.label} — `{_return_str(pos, dash='N/A')}`",
        "",
        f"- **Asset:** {pos.asset.capitalize()}",
        f"- **Closed:** {pos.close_date}",
    ]
    if pos.asset == "option":
        if pos.strategy:
            out.append(f"- **Strategy:** {pos.strategy}")
        if pos.strike:
            out.append(f"- **Strike:** {pos.strike}")
        if pos.expiry:
            out.append(f"- **Expiry:** {pos.expiry}")
    if show_dollar_amounts and pos.realized_pl is not None:
        out.append(f"- **Realized P/L:** `{pos.realized_pl:+.2f} {pos.currency}`")

    out.extend([
        "",
        "_Rationale & lessons: TODO — add your notes before publishing._",
        "",
    ])
    return out


# --------------------------------------------------------------------------- #
# Assemble the week's package
# --------------------------------------------------------------------------- #

def generate_weekly_journal(
    positions: list[ClosedPosition],
    week_ending: date,
    *,
    show_dollar_amounts: bool = False,
    blog_url: str = "",
) -> WeeklyJournal:
    """Build the single caption + single blog draft + one card per trade."""
    caption = format_weekly_caption(
        positions, week_ending, show_dollar_amounts=show_dollar_amounts, blog_url=blog_url
    )
    blog_draft = format_weekly_blog(
        positions, week_ending, show_dollar_amounts=show_dollar_amounts
    )
    cards = [
        (pos, render_card_svg(pos, show_dollar_amounts=show_dollar_amounts))
        for pos in positions
    ]
    return WeeklyJournal(
        week_ending=week_ending,
        positions=positions,
        show_dollar_amounts=show_dollar_amounts,
        caption=caption,
        blog_draft=blog_draft,
        cards=cards,
    )

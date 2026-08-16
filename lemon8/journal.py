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
from lemon8.card import render_trade_table_pages


@dataclass(frozen=True)
class WeeklyJournal:
    """One week's journal deliverable: a single caption + blog + table images.

    ``table_pages`` are 3:4 portrait SVGs — a trade-log table (one row per closed
    trade, both stocks and options) split across as many pages as it takes to
    stay readable. This is the post's image carousel.
    """

    week_ending: date
    positions: list[ClosedPosition]
    show_dollar_amounts: bool
    caption: str
    blog_draft: str
    table_pages: list[str]  # one SVG per portrait page


# --------------------------------------------------------------------------- #
# Small shared formatters
# --------------------------------------------------------------------------- #

def _bold(s: str) -> str:
    """Map ASCII letters/digits to Unicode sans-serif bold (𝗔-𝗭, 𝗮-𝘇, 𝟬-𝟵).

    Social captions (Lemon8/TikTok) are plain-text boxes that don't render
    Markdown, so ``**bold**`` shows the asterisks literally. Unicode bold survives
    a copy-paste and displays bold in the post. Only used in the caption — the
    blog is real Markdown. Never apply to hashtags: bold letters aren't matched as
    tags.
    """
    out = []
    for ch in s:
        o = ord(ch)
        if 0x41 <= o <= 0x5A:      # A-Z
            out.append(chr(0x1D5D4 + o - 0x41))
        elif 0x61 <= o <= 0x7A:    # a-z
            out.append(chr(0x1D5EE + o - 0x61))
        elif 0x30 <= o <= 0x39:    # 0-9
            out.append(chr(0x1D7EC + o - 0x30))
        else:
            out.append(ch)
    return "".join(out)


def _return_str(pos: ClosedPosition, *, dash: str = "Closed") -> str:
    return f"{pos.return_pct:+.1f}%" if pos.return_pct is not None else dash


def _md_cell(s: str) -> str:
    """Make free text safe inside a Markdown table cell: escape pipes and flatten
    newlines so a hand-typed Reason can't break the table layout."""
    return " ".join((s or "").split()).replace("|", "\\|").strip()


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

    A summary line + the biggest movers only — not every trade (a busy week is
    150+, which would be an unreadable caption; the full list lives in the table
    images). Percentage-only unless ``show_dollar_amounts=True``.
    """
    title = _bold(f"Weekly Trade Journal — week ending {week_ending:%d %b %Y}")
    if not positions:
        return f"📊 {title}\n\nNo trades closed this week. Staying patient. 🧘"

    wins = sum(1 for p in positions if p.is_win is True)
    losses = sum(1 for p in positions if p.is_win is False)
    lines = [
        f"📊 {title}",
        "",
        _bold(f"{len(positions)} trades closed · {wins} wins · {losses} losses"),
    ]

    movers = sorted((p for p in positions if p.return_pct is not None), key=lambda p: p.return_pct)
    if movers:
        top: list[ClosedPosition] = []
        for pos in movers[-3:][::-1] + movers[:3]:   # top winners, then top losers
            if pos not in top:                        # dedupe when few movers overlap
                top.append(pos)
        lines.append("")
        lines.append("Top movers:")
        for pos in top:
            head = f"{pos.label} ({pos.kind})" if pos.kind else pos.label
            line = f"{_win_emoji(pos)} {_bold(f'{head}: {_return_str(pos)}')}"
            if show_dollar_amounts and pos.realized_pl is not None:
                line += f" ({pos.realized_pl:+.2f} {pos.currency})"
            why = " ".join((pos.reason or "").split())
            if why:
                line += f" — {why[:60]}"
            lines.append(line)

    tags = " ".join(f"#{s}" for s in _unique_symbols(positions)[:12])
    lines.extend([
        "",
        "👇 Full trade log in the images · breakdown on the blog:",
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
    """Format the single Markdown blog draft for the week.

    A summary (counts), the biggest movers, and a full trade table — one row per
    closed trade, both stocks and options. Scales to a busy week (100+ trades)
    without becoming a hundred prose stubs; the rationale is a single TODO for
    the user to fill before publishing.
    """
    wins = sum(1 for p in positions if p.is_win is True)
    losses = sum(1 for p in positions if p.is_win is False)

    lines = [
        f"# Weekly Trading Journal — week ending {week_ending.isoformat()}",
        "",
    ]
    if not positions:
        lines.extend([
            "**No positions closed** this week.",
            "",
            "---",
            "_Generated automatically from portfolio sync logs._",
        ])
        return "\n".join(lines)

    lines.extend([
        f"**{len(positions)} position(s) closed** this week — "
        f"{wins} win(s), {losses} loss(es).",
        "",
    ])

    movers = sorted((p for p in positions if p.return_pct is not None), key=lambda p: p.return_pct)
    if movers:
        lines.append("## Biggest movers")
        lines.append("")
        for p in movers[-3:][::-1]:
            lines.append(f"- 🚀 **{p.label}** — `{_return_str(p, dash='N/A')}`")
        for p in movers[:3]:
            lines.append(f"- 📉 **{p.label}** — `{_return_str(p, dash='N/A')}`")
        lines.append("")

    lines.extend(_blog_trade_table(positions, show_dollar_amounts))

    lines.extend([
        "## Rationale & lessons",
        "",
        "_TODO — add your notes before publishing._",
        "",
        "---",
        "_Generated automatically from portfolio sync logs._",
    ])
    return "\n".join(lines)


def _blog_trade_table(positions: list[ClosedPosition], show_dollar_amounts: bool) -> list[str]:
    """A Markdown table of every closed trade, most-recent first.

    The ``Strategy / Action`` column carries the closest thing to a *why* the
    sheet records (option strategy or the stock's Buy/Sell). The free-text trade
    thesis lives in the ``Why`` column when the sheet's Reason cell is filled.
    """
    dollars = show_dollar_amounts
    header = "| Date | Instrument | Strategy / Action | Why | Result | Return % |"
    rule = "|------|------------|-------------------|-----|--------|----------|"
    if dollars:
        header = header[:-1] + " P/L |"
        rule = rule[:-1] + "------|"

    out = ["## All closed trades", "", header, rule]
    for p in sorted(positions, key=lambda p: (p.close_date, p.label), reverse=True):
        result = "Win" if p.is_win is True else ("Loss" if p.is_win is False else "—")
        pct = _return_str(p, dash="—")
        why = _md_cell(p.reason) or "—"
        row = (f"| {p.close_date} | {p.label} | {p.kind or '—'} | {why} "
               f"| {result} | {pct} |")
        if dollars:
            pl = f"{p.realized_pl:+.2f} {p.currency}" if p.realized_pl is not None else "—"
            row = row[:-1] + f" {pl} |"
        out.append(row)
    out.append("")
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
    table_pages = render_trade_table_pages(positions, week_ending)
    return WeeklyJournal(
        week_ending=week_ending,
        positions=positions,
        show_dollar_amounts=show_dollar_amounts,
        caption=caption,
        blog_draft=blog_draft,
        table_pages=table_pages,
    )

"""Lemon8 weekly trading-journal job (step 10 — BUILD_SPEC.md §11b).

Ties the generator pieces together into ONE weekly deliverable — not one post
per trade:

1. Read the sheet's ``Closed`` positions (``lemon8.reader``).
2. Keep those closed in the last 7 days — a true weekly slice.
3. Generate a single journal for the week (``lemon8.journal``): one caption +
   one blog draft + one screenshot card per trade, written to
   ``lemon8_out/<week>/`` (blog.md, caption.txt, cards/<slug>.svg|png).
4. Optionally commit the ONE weekly blog draft to the blog repo as a DRAFT
   (``lemon8.blog``) — skipped fail-soft when GitHub isn't configured.
5. Telegram a heads-up that the week's journal is ready.

No auto-posting to Lemon8/TikTok — those have no posting API (§11b); the user
uploads the cards + caption manually. Runs on its own Sunday-evening cadence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from lemon8.reader import read_closed_positions, ClosedPosition
from lemon8.journal import generate_weekly_journal, WeeklyJournal
from lemon8.render import svg_to_png, RenderError
from lemon8.blog import commit_blog_drafts, CommittedDraft, BlogCommitError
from alerting.notify import notify_safe

log = logging.getLogger(__name__)

DEFAULT_WINDOW_DAYS = 7
OUTPUT_ROOT = "lemon8_out"


@dataclass(frozen=True)
class WeeklyJournalResult:
    count: int          # trades in the week's journal (cards generated)
    out_dir: str        # where the files were written
    committed: int      # blog drafts committed to GitHub (0 or 1 — one per week)
    delivered: bool     # Telegram heads-up delivered


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def select_recent_closes(
    positions: list[ClosedPosition], *, today: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> list[ClosedPosition]:
    """Positions whose close date falls in ``[today - window_days, today]``."""
    cutoff = today - timedelta(days=window_days)
    out = [p for p in positions if _in_window(p.close_date, cutoff, today)]
    out.sort(key=lambda p: (p.close_date, p.label))
    return out


def _in_window(close_date: str, cutoff: date, today: date) -> bool:
    d = _parse_iso(close_date)
    return d is not None and cutoff <= d <= today


def _parse_iso(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (ValueError, TypeError):
        return None


def _slug(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
    return s or "trade"


# --------------------------------------------------------------------------- #
# File output
# --------------------------------------------------------------------------- #

def _write_weekly_files(journal: WeeklyJournal, out_dir: Path, *, render_png: bool = True) -> None:
    """Write the week's single caption + blog, and one card per trade under
    ``cards/``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "caption.txt").write_text(journal.caption, encoding="utf-8")
    (out_dir / "blog.md").write_text(journal.blog_draft, encoding="utf-8")

    if journal.cards:
        cards_dir = out_dir / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        for pos, svg in journal.cards:
            slug = _slug(pos.label)
            (cards_dir / f"{slug}.svg").write_text(svg, encoding="utf-8")
            if render_png:
                try:
                    (cards_dir / f"{slug}.png").write_bytes(svg_to_png(svg))
                except RenderError:
                    # A PNG hiccup must not lose the card — SVG is kept.
                    log.warning("PNG render failed for %s; SVG kept.", slug, exc_info=True)


def _write_index(out_dir: Path, journal: WeeklyJournal, today: date, window_days: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Trade journal — week ending {today.isoformat()}",
        "",
        f"{len(journal.positions)} position(s) closed in the last {window_days} days.",
        "",
        "- `blog.md` — the week's consolidated retrospective (single post)",
        "- `caption.txt` — the single Lemon8 / TikTok caption",
        "- `cards/` — one screenshot card per trade (the image carousel)",
        "",
    ]
    for pos, _svg in journal.cards:
        lines.append(f"- **{pos.label}** ({_return_str(pos)}) — `cards/{_slug(pos.label)}.png`")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _return_str(pos: ClosedPosition) -> str:
    return f"{pos.return_pct:+.1f}%" if pos.return_pct is not None else "Closed"


# --------------------------------------------------------------------------- #
# Blog draft file (frontmatter + body)
# --------------------------------------------------------------------------- #

def _weekly_blog_file(journal: WeeklyJournal) -> tuple[str, str]:
    """(filename, markdown) for the ONE weekly GitHub draft commit. ``draft: true``
    keeps it unpublished even if the drafts branch is ever merged/built by
    accident."""
    on = journal.week_ending
    frontmatter = "\n".join([
        "---",
        f'title: "Weekly Trading Journal — week ending {on.isoformat()}"',
        f"date: {on.isoformat()}",
        "draft: true",
        "tags: [trading, weekly-journal]",
        "---",
        "",
    ])
    return f"{on.isoformat()}-weekly-journal.md", frontmatter + journal.blog_draft + "\n"


# --------------------------------------------------------------------------- #
# Telegram heads-up
# --------------------------------------------------------------------------- #

def _summary_message(
    journal: WeeklyJournal, today: date, out_dir: Path, committed: list[CommittedDraft]
) -> str:
    positions = journal.positions
    if not positions:
        return (
            f"\U0001f4d3 Lemon8: no trades closed in the last 7 days "
            f"(week ending {today:%d %b %Y})."
        )
    lines = [
        f"\U0001f4d3 Lemon8: weekly journal ready ({len(positions)} trade(s)) — "
        f"week ending {today:%d %b %Y}:",
        "",
    ]
    for pos in positions:
        lines.append(f"   • {pos.label} ({_return_str(pos)})")
    lines.append("")
    lines.append(f"Files: {out_dir}  (blog.md + caption.txt + {len(journal.cards)} card(s))")
    if committed:
        lines.append("Weekly blog draft committed.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration + CLI
# --------------------------------------------------------------------------- #

def run_weekly_journal(
    client,
    *,
    today: Optional[date] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    output_root: str = OUTPUT_ROOT,
    notifier: Callable[[str], bool] = notify_safe,
    blog_settings: Optional[dict] = None,
    blog_committer: Optional[Callable] = None,
    render_png: bool = True,
) -> WeeklyJournalResult:
    """Generate the week's journal packages, write files, optionally commit blog
    drafts, and send the Telegram heads-up. ``client`` is a ``SheetClient`` (or a
    fake with ``get_values``)."""
    today = today or date.today()

    recent = select_recent_closes(read_closed_positions(client), today=today, window_days=window_days)
    journal = generate_weekly_journal(recent, today)

    out_dir = Path(output_root) / today.isoformat()
    _write_weekly_files(journal, out_dir, render_png=render_png)
    _write_index(out_dir, journal, today, window_days)

    committed = _maybe_commit_blog(journal, blog_settings, blog_committer)

    delivered = notifier(_summary_message(journal, today, out_dir, committed))
    return WeeklyJournalResult(
        count=len(journal.positions), out_dir=str(out_dir),
        committed=len(committed), delivered=delivered,
    )


def _maybe_commit_blog(
    journal: WeeklyJournal,
    blog_settings: Optional[dict],
    blog_committer: Optional[Callable],
) -> list[CommittedDraft]:
    if not journal.positions:
        return []
    if blog_settings is None:
        from config.settings import get_blog_settings
        blog_settings = get_blog_settings()
    if not (blog_settings.get("token") and blog_settings.get("repo")):
        log.info("Blog commit skipped (GITHUB_TOKEN / BLOG_REPO not configured).")
        return []

    committer = blog_committer or commit_blog_drafts
    items = [_weekly_blog_file(journal)]  # ONE blog per week
    try:
        return committer(items, settings=blog_settings)
    except BlogCommitError:
        log.warning("Blog draft commit failed; files still written locally.", exc_info=True)
        return []


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from config.settings import get_service_account_info, get_spreadsheet_id
    from sheets.writer import SheetClient

    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    result = run_weekly_journal(client)

    log.info(
        "Lemon8 weekly: %d trade(s) in journal, %d blog draft(s) committed, delivered=%s, out=%s",
        result.count, result.committed, result.delivered, result.out_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

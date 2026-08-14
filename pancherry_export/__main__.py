"""CLI: refresh the pancherry site's data files from the live Sheet.

    python -m pancherry_export            # write into the default clone
    python -m pancherry_export --dry-run  # report what would change, write nothing

Regenerates ``openPositions.ts`` and appends this week's journal draft
(``published: false``). Sends a Telegram heads-up to review + push. Never
commits or pushes — publishing the public site stays a manual step.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from alerting.notify import notify_safe
from config.settings import (
    get_pancherry_repo,
    get_service_account_info,
    get_spreadsheet_id,
)
from lemon8.reader import read_closed_positions
from pancherry_export.exporter import (
    build_weekly_journal,
    read_open_positions,
    render_open_positions_ts,
    render_journal_entry,
    upsert_journal_entry,
    write_open_positions,
)
from sheets.writer import SheetClient

log = logging.getLogger(__name__)

_OPEN_REL = Path("src/data/openPositions.ts")
_JOURNAL_REL = Path("src/data/weeklyJournals.ts")


def run(client, repo: Path, *, today: date, dry_run: bool = False, notifier=notify_safe) -> int:
    open_path = repo / _OPEN_REL
    journal_path = repo / _JOURNAL_REL
    for p in (open_path, journal_path):
        if not p.exists():
            raise SystemExit(f"Expected pancherry data file not found: {p}")

    positions = read_open_positions(client)
    entry = build_weekly_journal(read_closed_positions(client), today=today)

    if dry_run:
        log.info("[dry-run] openPositions: %d underlyings", len(positions))
        log.info("[dry-run] journal %s: %d trades, %dW/%dL, %d%%",
                 entry["slug"], entry["trades"], entry["wins"], entry["losses"], entry["winRatePct"])
        print(render_open_positions_ts(positions)[:400] + "\n...")
        print(render_journal_entry(entry))
        return 0

    write_open_positions(positions, open_path)
    inserted = upsert_journal_entry(entry, journal_path)

    leg_count = sum(len(p.legs) for p in positions)
    msg = (
        f"\U0001f4ca pancherry draft ready — {date.today():%d %b %Y}\n"
        f"   • Open positions: {len(positions)} underlyings, {leg_count} option legs\n"
        f"   • Journal {entry['slug']}: {entry['trades']} trades, "
        f"{entry['wins']}W/{entry['losses']}L, {entry['winRatePct']}% "
        + ("(new draft)" if inserted else "(already present — skipped)")
        + f"\n\nReview & push:\n   cd {repo}\n   git diff"
    )
    delivered = notifier(msg)
    log.info(
        "pancherry export: %d positions, journal %s %s, telegram delivered=%s",
        len(positions), entry["slug"], "inserted" if inserted else "skipped", delivered,
    )
    print(msg)
    return 0


def _force_utf8() -> None:
    """The summary + logs carry emoji, but the Windows console (and a redirected
    task log) default to cp1252 and raise UnicodeEncodeError. Reconfigure both
    streams to UTF-8 so output never crashes the run after the work is done."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _force_utf8()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Refresh pancherry data files from the live Sheet.")
    parser.add_argument("--repo", default=get_pancherry_repo(), help="Path to the pancherry clone.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    args = parser.parse_args(argv)

    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    return run(client, Path(args.repo), today=date.today(), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

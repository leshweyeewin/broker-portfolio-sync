"""CLI: refresh the pancherry site's data files from the live Sheet.

    python -m pancherry_export            # write into the default clone
    python -m pancherry_export --dry-run  # report what would change, write nothing
    python -m pancherry_export --pr       # ...then commit + open a Draft PR

Regenerates ``openPositions.ts`` and appends this week's journal draft
(``published: true``); a re-run refreshes only the stat tiles in place. Without
``--pr`` it just writes the local clone (review + push by hand). With ``--pr`` it
commits the local files to a drafts branch and opens/updates a Draft PR — the
review gate — so the weekly flow is push-free. Never merges: publishing is the
merge you control.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from alerting.notify import notify_safe
from config.settings import (
    get_pancherry_gh_settings,
    get_pancherry_repo,
    get_service_account_info,
    get_spreadsheet_id,
    get_ticker_names_path,
)
from core.ticker_names import load_names
from lemon8.reader import read_closed_positions
from pancherry_export.exporter import (
    assess_journal_drift,
    build_weekly_journal,
    read_open_positions,
    refresh_journal_stats,
    render_open_positions_ts,
    render_journal_entry,
    upsert_journal_entry,
    write_open_positions,
)
from pancherry_export.publish import PancherryPublishError, publish_draft_pr
from sheets.writer import SheetClient

log = logging.getLogger(__name__)

_OPEN_REL = Path("src/data/openPositions.ts")
_JOURNAL_REL = Path("src/data/weeklyJournals.ts")


def run(client, repo: Path, *, today: date, dry_run: bool = False,
        open_pr: bool = False, pr_settings: dict | None = None, notifier=notify_safe) -> int:
    open_path = repo / _OPEN_REL
    journal_path = repo / _JOURNAL_REL
    for p in (open_path, journal_path):
        if not p.exists():
            raise SystemExit(f"Expected pancherry data file not found: {p}")

    positions = read_open_positions(client)
    entry = build_weekly_journal(read_closed_positions(client), today=today)
    names = load_names(get_ticker_names_path())

    if dry_run:
        named = sum(1 for p in positions if names.get(p.ticker))
        log.info("[dry-run] openPositions: %d underlyings (%d named)", len(positions), named)
        log.info("[dry-run] journal %s: %d trades, %dW/%dL, %d%%",
                 entry["slug"], entry["trades"], entry["wins"], entry["losses"], entry["winRatePct"])
        print(render_open_positions_ts(positions, names=names)[:400] + "\n...")
        print(render_journal_entry(entry))
        return 0

    write_open_positions(positions, open_path, names=names)

    # Read drift from the pre-refresh file, then update. First run of a week
    # inserts a full draft; a re-run refreshes only the stat tiles in place
    # (prose/highlights survive) so the review PR stays current.
    drift = assess_journal_drift(entry, journal_path)
    if upsert_journal_entry(entry, journal_path):
        j_status = "new draft"
    elif drift is not None and entry["trades"] < drift.prev_trades:
        # Trades can only accumulate within a week — a drop means a stale/glitched
        # read (e.g. dates came back unparseable). Never overwrite good stats with it.
        j_status = f"refresh SKIPPED — {entry['trades']} trades < existing {drift.prev_trades} (stale read?)"
        log.warning("Refusing to refresh %s: %d trades < existing %d.",
                    entry["slug"], entry["trades"], drift.prev_trades)
    else:
        j_status = "stats refreshed" if refresh_journal_stats(entry, journal_path) else "already current"

    drift_note = _drift_note(drift)
    leg_count = sum(len(p.legs) for p in positions)
    summary = (
        f"\U0001f4ca pancherry draft ready — {date.today():%d %b %Y}\n"
        f"   • Open positions: {len(positions)} underlyings, {leg_count} option legs\n"
        f"   • Journal {entry['slug']}: {entry['trades']} trades, "
        f"{entry['wins']}W/{entry['losses']}L, {entry['winRatePct']}% ({j_status})"
    )
    if drift_note:
        summary += f"\n   • {drift_note}"

    if open_pr:
        pr_line = _do_pr(entry, [open_path, journal_path], j_status, drift_note,
                         pr_settings or get_pancherry_gh_settings())
        msg = summary + f"\n\n{pr_line}"
    else:
        msg = summary + f"\n\nReview & push:\n   cd {repo}\n   git diff"

    delivered = notifier(msg)
    log.info(
        "pancherry export: %d positions, journal %s (%s), pr=%s, telegram delivered=%s",
        len(positions), entry["slug"], j_status, open_pr, delivered,
    )
    print(msg)
    return 0


def _drift_note(drift) -> str:
    """One-line 'the story may be stale' warning, or '' when nothing drifted."""
    if drift is None or not drift.grew:
        return ""
    note = (f"⚠️ {drift.added} more trade(s) closed since the draft "
            f"(was {drift.prev_trades}, now {drift.new_trades}) — prose & highlights may need a revise.")
    if drift.top_winner:
        note += f" Top winner now: {drift.top_winner}."
    if drift.top_loser:
        note += f" Top loser now: {drift.top_loser}."
    return note


def _do_pr(entry: dict, local_paths: list[Path], j_status: str, drift_note: str, settings: dict) -> str:
    """Commit the local files + open/update the Draft PR; return a status line."""
    title = f"pancherry weekly draft — {entry['slug']}"
    body = "\n".join([
        f"Auto-generated from the live sheet ({j_status}).",
        "",
        f"- **Journal {entry['slug']}** — {entry['trades']} trades, "
        f"{entry['wins']}W/{entry['losses']}L, {entry['winRatePct']}% win rate",
        "- `openPositions.ts` fully regenerated from the current book",
        "",
        (f"> {drift_note}" if drift_note else
         "_Stat tiles reflect the latest data. Prose & highlights are yours — edit on this branch before merging._"),
        "",
        "Merging this publishes to the live site. Nothing goes live until you merge.",
    ])
    files = [(_OPEN_REL.as_posix(), str(local_paths[0])), (_JOURNAL_REL.as_posix(), str(local_paths[1]))]
    try:
        result = publish_draft_pr(files, settings=settings, title=title, body=body)
    except PancherryPublishError as exc:
        log.warning("Auto-PR failed: %s", exc)
        return f"❌ Auto-PR failed: {exc}"
    if not result.url:
        return "✅ Nothing changed — no PR needed."
    verb = "opened" if result.created else "updated"
    return f"\U0001f517 Draft PR {verb} ({result.committed} file(s)):\n   {result.url}"


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
    parser.add_argument("--pr", action="store_true",
                        help="After writing, commit the files to a drafts branch and open/update a Draft PR.")
    parser.add_argument("--date", help="Override the 'today' date (YYYY-MM-DD) for generating past weeks.")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.date) if args.date else date.today()
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    return run(client, Path(args.repo), today=today, dry_run=args.dry_run, open_pr=args.pr)


if __name__ == "__main__":
    raise SystemExit(main())

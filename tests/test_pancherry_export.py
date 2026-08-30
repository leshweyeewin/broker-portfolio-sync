"""Tests for pancherry_export — the Phase 2 data-file generator.

Fully offline: the Sheet is a FakeSheetClient, journal input is built from
ClosedPosition values directly, and file writes go to tmp_path.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from lemon8.reader import ClosedPosition, read_closed_positions
from sheets.writer import sheet_date_to_iso
from pancherry_export.exporter import (
    OpenPositionData,
    assess_journal_drift,
    build_weekly_journal,
    read_open_positions,
    refresh_journal_stats,
    render_journal_entry,
    render_open_positions_ts,
    upsert_journal_entry,
    write_open_positions,
)
from sheets.writer import STOCKS_HEADERS, OPTIONS_HEADERS, TAB_STOCKS, TAB_OPTIONS
from tests.test_writer import FakeSheetClient

_SUMMARY_BLOCK = [["Total P/L", ""], ["Total Fees", ""]]
_TODAY = date(2026, 8, 17)


# STOCKS_HEADERS: Date, Broker, Ticker, Action, Qty, Price, Total, Fee,
#                 Currency, Status, Realized P/L, Realized P/L (SGD), _dedup_key
def _stock(broker, ticker, action, qty, status="Open"):
    return ["2026-08-11", broker, ticker, action, qty, 10.0, 100.0, 1.0, "USD",
            status, "", "", f"{ticker}-{action}-{qty}"]


# OPTIONS_HEADERS: Date, Broker, Strategy, Stock, Type, Strike, Qty, Expiry,
#   Action, Premium, Total, Fee, Currency, Status, P/L, P/L (SGD), _dedup_key
def _opt(broker, stock, otype, strike, qty, expiry, action, *, status="Open", strategy=None):
    return ["2026-08-11", broker, strategy or f"{action} {otype}", stock, otype, strike, qty,
            expiry, action, 1.0, 100.0, 1.0, "USD", status, "", "", f"{stock}-{action}-{expiry}"]


def _client(*, stocks=(), options=()) -> FakeSheetClient:
    client = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])
    client.batch_update_values([
        {"range": f"{TAB_STOCKS}!A1",
         "values": _SUMMARY_BLOCK + [[str(h) for h in STOCKS_HEADERS]] + list(stocks)},
        {"range": f"{TAB_OPTIONS}!A1",
         "values": _SUMMARY_BLOCK + [[str(h) for h in OPTIONS_HEADERS]] + list(options)},
    ])
    return client


# --------------------------------------------------------------------------- #
# read_open_positions
# --------------------------------------------------------------------------- #

def test_nets_shares_and_drops_closed_out_ticker():
    client = _client(stocks=[
        _stock("Tiger", "GOOG", "Buy", 30),
        _stock("Tiger", "GOOG", "Sell", 10),         # net GOOG = 20
        _stock("Tiger", "NVDA", "Buy", 5),
        _stock("Tiger", "NVDA", "Sell", 5),          # net 0 → dropped
    ])
    out = read_open_positions(client)
    assert [(p.ticker, p.shares) for p in out] == [("GOOG", Decimal(20))]


def test_nets_option_legs_and_signs_by_action():
    client = _client(options=[
        _opt("Tiger", "SNDK", "Call", "$1400.00", 4, "2026-08-14", "Sell"),   # short 4
        _opt("Tiger", "SNDK", "Call", "$1405.00", 3, "2026-08-14", "Buy"),    # long 3
        _opt("Tiger", "SNDK", "Call", "$1405.00", 1, "2026-08-14", "Sell"),   # net 1405 = 2
    ])
    out = read_open_positions(client)
    assert len(out) == 1
    legs = {(l.strike, l.qty) for l in out[0].legs}
    assert legs == {(Decimal(1400), Decimal(-4)), (Decimal(1405), Decimal(2))}


def test_skips_malformed_combo_underlying():
    # A spread's combo symbol leaked into both the Stock and Ticker columns —
    # must not become a card from either the shares or the legs path.
    client = _client(
        stocks=[_stock("Tiger", "SHOP260821P130/145", "Buy", 1)],
        options=[
            _opt("Tiger", "SHOP260821P130/145", "Put", "$130.00", 1, "2026-08-21", "Sell"),
            _opt("Tiger", "SHOP", "Put", "$145.00", 1, "2026-08-21", "Sell"),
        ],
    )
    tickers = [p.ticker for p in read_open_positions(client)]
    assert tickers == ["SHOP"]


# --------------------------------------------------------------------------- #
# Sheets serial-date handling (get_values returns userEnteredValue serials)
# --------------------------------------------------------------------------- #

def test_sheet_date_to_iso_handles_serials_strings_and_junk():
    assert sheet_date_to_iso(46243) == "2026-08-09"     # Sheets serial → ISO
    assert sheet_date_to_iso("2026-08-10") == "2026-08-10"
    assert sheet_date_to_iso("") == ""
    assert sheet_date_to_iso("not-a-date") == "not-a-date"


def test_closed_rows_with_serial_dates_are_still_windowed():
    # Date cells can come back as serial numberValues; the reader must parse them
    # or every row falls out of the window (the 0-trades bug).
    row = [46243, "Tiger", "GOOG", "Sell", 10, 10.0, 110.0, 1.0, "USD",
           "Closed", 10.0, "", "GOOG-x"]                 # Date = serial for 2026-08-09
    client = _client(stocks=[row])

    closed = read_closed_positions(client)
    assert closed[0].close_date == "2026-08-09"

    j = build_weekly_journal(closed, today=date(2026, 8, 12), window_days=7)
    assert j["trades"] == 1


def test_open_leg_expiry_serial_is_rendered_as_iso():
    client = _client(options=[
        # Expiry column (index 7) as a serial for 2026-08-15.
        ["2026-08-11", "Tiger", "Sell Call", "AVGO", "Call", "$400.00", -2, 46249,
         "Sell", 1.0, 100.0, 1.0, "USD", "Open", "", "", "AVGO-x"],
    ])
    legs = read_open_positions(client)[0].legs
    assert legs[0].expiry == "2026-08-15"


def test_ticker_with_only_options_still_appears():
    client = _client(options=[
        _opt("Tiger", "PLTR", "Put", "$40.00", 1, "2026-09-18", "Sell"),
    ])
    out = read_open_positions(client)
    assert out[0].ticker == "PLTR"
    assert out[0].shares == Decimal(0)
    assert out[0].legs[0].qty == Decimal(-1)


# --------------------------------------------------------------------------- #
# render_open_positions_ts
# --------------------------------------------------------------------------- #

def test_render_open_positions_shape_and_hidden_flag():
    positions = [
        OpenPositionData(ticker="GOOG", shares=Decimal(20), legs=[]),
        OpenPositionData(ticker="SNDK", shares=Decimal(0), legs=[]),
    ]
    ts = render_open_positions_ts(positions, hidden={"SNDK"})
    assert "export const openPositions: OpenPosition[] = [" in ts
    assert "{ ticker: 'GOOG', shares: 20, legs: [] }," in ts
    assert "{ ticker: 'SNDK', shares: 0, hidden: true, legs: [] }," in ts
    assert ts.rstrip().endswith("];")


def test_render_open_positions_emits_names_when_provided():
    positions = [
        OpenPositionData(ticker="MSFT", shares=Decimal(10), legs=[]),
        OpenPositionData(ticker="07709", shares=Decimal(400), legs=[]),   # no name in map
    ]
    ts = render_open_positions_ts(positions, names={"MSFT": "Microsoft"})
    assert "name?: string;" in ts   # interface carries the optional field
    assert "{ ticker: 'MSFT', name: \"Microsoft\", shares: 10, legs: [] }," in ts
    # A ticker with no cached name renders without a name field.
    assert "{ ticker: '07709', shares: 400, legs: [] }," in ts


def test_write_open_positions_preserves_existing_hidden(tmp_path):
    path = tmp_path / "openPositions.ts"
    path.write_text(
        "export const openPositions: OpenPosition[] = [\n"
        "  { ticker: 'NIO', shares: 100, hidden: true, legs: [] },\n"
        "];\n",
        encoding="utf-8",
    )
    write_open_positions([OpenPositionData(ticker="NIO", shares=Decimal(120), legs=[])], path)
    text = path.read_text(encoding="utf-8")
    assert "{ ticker: 'NIO', shares: 120, hidden: true, legs: [] }," in text


# --------------------------------------------------------------------------- #
# build_weekly_journal
# --------------------------------------------------------------------------- #

def _closed(symbol, pl, *, asset="option", return_pct=None, close="2026-08-13",
            otype="Call", strike="$1405.00", strategy="Long Call"):
    return ClosedPosition(
        broker="Tiger", symbol=symbol, asset=asset, close_date=close, currency="USD",
        realized_pl=Decimal(pl), realized_pl_sgd=None,
        return_pct=Decimal(return_pct) if return_pct is not None else None,
        option_type=otype if asset == "option" else "",
        strike=strike if asset == "option" else "",
        expiry="2026-08-14" if asset == "option" else "",
        strategy=strategy,
    )


def test_journal_stats_and_highlights():
    closed = [
        _closed("SNDK", 500, return_pct="230.6"),
        _closed("MSFT", 120, return_pct="46.6", strike="$510.00"),
        _closed("BE", -80, return_pct="-38.3", strike="$240.00", strategy="Long Call"),
        _closed("NOPE", 10, close="2026-07-01"),   # out of window → excluded
    ]
    j = build_weekly_journal(closed, today=_TODAY, window_days=7)

    assert j["trades"] == 3
    assert j["wins"] == 2
    assert j["losses"] == 1
    assert j["winRatePct"] == 67
    assert j["published"] is True
    assert j["slug"].startswith("2026-w")

    # Grouped alphabetically by ticker, then by P/L desc.
    assert j["highlights"][0]["ticker"] == "BE"
    assert j["highlights"][0]["trades"][0]["direction"] == "loss"
    assert j["highlights"][0]["trades"][0]["returnPct"] == -38.3
    
    # SNDK will be the last one alphabetically in this set
    assert j["highlights"][2]["ticker"] == "SNDK"
    assert j["highlights"][2]["trades"][0]["direction"] == "win"
    assert j["highlights"][2]["trades"][0]["contract"] == "$1,405 Call"
    assert j["highlights"][2]["trades"][0]["returnPct"] == 230.6


def test_journal_handles_empty_week():
    j = build_weekly_journal([], today=_TODAY, window_days=7)
    assert j["trades"] == 0
    assert j["winRatePct"] == 0
    assert j["highlights"] == []


# --------------------------------------------------------------------------- #
# render_journal_entry + upsert
# --------------------------------------------------------------------------- #

def test_render_journal_entry_escapes_prose():
    entry = build_weekly_journal([_closed("SNDK", 500, return_pct="230.6")], today=_TODAY)
    entry["body"] = ["It's a test with \"quotes\" and an apostrophe."]
    text = render_journal_entry(entry)
    # json.dumps keeps the string valid — the raw apostrophe must not appear unescaped
    # inside a single-quoted literal (it's double-quoted here).
    assert '"It\'s a test with \\"quotes\\" and an apostrophe."' in text
    assert text.strip().startswith("{")


def _journal_file(tmp_path, *slugs):
    # Ensure journals directory exists
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir(parents=True, exist_ok=True)
    
    body = "export const weeklyJournals: WeeklyJournal[] = [\n"
    for slug in slugs:
        var_name = f"journal_{slug.replace('-', '_')}"
        body += f"  {var_name},\n"
        
        # Create a mock journal file for this slug
        mock_journal = f"export const {var_name}: WeeklyJournal = {{\n  slug: '{slug}',\n  trades: 56,\n}};\n"
        (journals_dir / f"{slug}.ts").write_text(mock_journal, encoding="utf-8")
        
    body += "];\n"
    path = tmp_path / "weeklyJournals.ts"
    path.write_text(body, encoding="utf-8")
    return path


def test_upsert_inserts_new_entry_at_top(tmp_path):
    path = _journal_file(tmp_path, "2026-w32")
    entry = build_weekly_journal([_closed("SNDK", 500, return_pct="230.6")], today=_TODAY)
    entry["slug"] = "2026-w33"

    assert upsert_journal_entry(entry, path) is True
    
    # Check that new file was created
    new_journal = tmp_path / "journals" / "2026-w33.ts"
    assert new_journal.exists()
    assert "slug: \"2026-w33\"" in new_journal.read_text(encoding="utf-8")
    
    # Check index file updated
    text = path.read_text(encoding="utf-8")
    open_at = text.index("weeklyJournals: WeeklyJournal[] = [")
    new_at = text.index("journal_2026_w33,")
    old_at = text.index("journal_2026_w32,")
    assert open_at < new_at < old_at   # newest inserted above the existing entry


def test_upsert_is_idempotent_on_duplicate_slug(tmp_path):
    path = _journal_file(tmp_path, "2026-w33")
    entry = build_weekly_journal([_closed("SNDK", 500, return_pct="230.6")], today=_TODAY)
    entry["slug"] = "2026-w33"

    assert upsert_journal_entry(entry, path) is False


# --------------------------------------------------------------------------- #
# refresh_journal_stats — in-place stat refresh on an existing (edited) entry
# --------------------------------------------------------------------------- #

# A hand-edited entry: single-quote style, real prose, curated highlights, and a
# number hard-coded into the prose (which must be left as-is — documented caveat).
_HANDWRITTEN = """export const journal_2026_w33: WeeklyJournal = {
  slug: '2026-w33',
  title: 'Storage Cycle Runs',
  weekOf: 'Aug 10–14, 2026',
  startDate: '2026-08-10',
  endDate: '2026-08-14',
  summary:
    'A hand-written summary that must survive the refresh.',
  trades: 56,
  wins: 34,
  losses: 22,
  winRatePct: 60,
  body: [
    'We took 56 trades this week. This sentence is prose, so the 56 stays 56 even ' +
    'if the tile refreshes to 57. The stats below are the machine fields.'
  ],
  highlights: [
    { ticker: 'SNDK', trades: [
        { asset: 'option', strategy: 'Long Call', contract: '$1,405 Call', direction: 'win', returnPct: 230.6, note: 'Storage-cycle breakout.' }
    ] },
    { ticker: 'BE', trades: [
        { asset: 'option', strategy: 'Long Call', contract: '$240 Call', direction: 'loss', returnPct: -38.3, note: 'Cut per risk rules.' }
    ] }
  ],
  published: false,
};
"""


def test_refresh_updates_stat_tiles_but_keeps_prose(tmp_path):
    # Set up index
    path = tmp_path / "weeklyJournals.ts"
    path.write_text("export const weeklyJournals: WeeklyJournal[] = [];", encoding="utf-8")
    
    # Set up individual journal file
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir()
    (journals_dir / "2026-w33.ts").write_text(_HANDWRITTEN, encoding="utf-8")

    assert refresh_journal_stats(_fresh_stats(), path) is True

    # Read back the updated file
    updated = (journals_dir / "2026-w33.ts").read_text(encoding="utf-8")

    # The machine-owned numbers updated:
    assert "trades: 70," in updated
    assert "wins: 45," in updated
    assert "winRatePct: 64," in updated
    assert "endDate: '2026-08-15'," in updated

    # But the prose fields were untouched (including the hand-written '56' in body):
    assert "We took 56 trades this week" in updated
    assert "Storage Cycle Runs" in updated
    assert "Storage-cycle breakout" in updated
    assert "published: false" in updated


def test_refresh_is_noop_if_stats_match(tmp_path):
    path = tmp_path / "weeklyJournals.ts"
    path.write_text("export const weeklyJournals: WeeklyJournal[] = [];", encoding="utf-8")
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir()
    (journals_dir / "2026-w33.ts").write_text(_HANDWRITTEN, encoding="utf-8")

    entry = _fresh_stats(trades=56, wins=34, losses=22, winRatePct=60, endDate="2026-08-14", weekOf="Aug 10–14, 2026")
    assert refresh_journal_stats(entry, path) is False


def _fresh_stats(**over):
    entry = {
        "slug": "2026-w33", "weekOf": "Aug 10–15, 2026",
        "startDate": "2026-08-10", "endDate": "2026-08-15",
        "trades": 70, "wins": 45, "losses": 25, "winRatePct": 64,
        # prose fields present but refresh must ignore them
        "title": "NOPE", "summary": "NOPE", "body": ["NOPE"],
        "highlights": [], "published": False,
    }
    entry.update(over)
    return entry




def test_refresh_noops_when_stats_already_current(tmp_path):
    """No change → returns False so the self-updating PR doesn't churn."""
    path = tmp_path / "weeklyJournals.ts"
    path.write_text("export const weeklyJournals: WeeklyJournal[] = [];", encoding="utf-8")
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir()
    (journals_dir / "2026-w33.ts").write_text(_HANDWRITTEN, encoding="utf-8")

    already = _fresh_stats(
        weekOf="Aug 10–14, 2026", endDate="2026-08-14",
        trades=56, wins=34, losses=22, winRatePct=60,
    )
    assert refresh_journal_stats(already, path) is False


# --------------------------------------------------------------------------- #
# assess_journal_drift — "the story may be stale" signal
# --------------------------------------------------------------------------- #

def test_drift_reports_growth_and_current_standouts(tmp_path):
    path = tmp_path / "weeklyJournals.ts"
    path.write_text("export const weeklyJournals: WeeklyJournal[] = [];", encoding="utf-8")
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir()
    (journals_dir / "2026-w33.ts").write_text(_HANDWRITTEN, encoding="utf-8")   # stored trades: 56

    entry = _fresh_stats(highlights=[
        {"ticker": "SNDK", "contract": "$1,405 Call", "direction": "win", "returnPct": 230.6},
        {"ticker": "BE", "contract": "$240 Call", "direction": "loss", "returnPct": -38.3},
    ])
    drift = assess_journal_drift(entry, path)

    assert drift.prev_trades == 56
    assert drift.new_trades == 70
    assert drift.grew is True
    assert drift.added == 14
    assert drift.top_winner == "SNDK $1,405 Call (+230.6%)"
    assert drift.top_loser == "BE $240 Call (-38.3%)"


def test_drift_is_none_when_slug_absent(tmp_path):
    path = tmp_path / "weeklyJournals.ts"
    path.write_text(_HANDWRITTEN, encoding="utf-8")
    assert assess_journal_drift(_fresh_stats(slug="2026-w99"), path) is None


def test_drift_not_grown_when_counts_equal(tmp_path):
    path = tmp_path / "weeklyJournals.ts"
    path.write_text("export const weeklyJournals: WeeklyJournal[] = [];", encoding="utf-8")
    journals_dir = tmp_path / "journals"
    journals_dir.mkdir()
    (journals_dir / "2026-w33.ts").write_text(_HANDWRITTEN, encoding="utf-8")
    
    drift = assess_journal_drift(_fresh_stats(trades=56, highlights=[]), path)
    assert drift.grew is False
    assert drift.added == 0

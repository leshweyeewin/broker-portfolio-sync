"""Tests for pancherry_export — the Phase 2 data-file generator.

Fully offline: the Sheet is a FakeSheetClient, journal input is built from
ClosedPosition values directly, and file writes go to tmp_path.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from lemon8.reader import ClosedPosition
from pancherry_export.exporter import (
    OpenPositionData,
    build_weekly_journal,
    read_open_positions,
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
    assert j["published"] is False
    assert j["slug"].startswith("2026-w")

    # Winners first (by P/L desc), then losers; biggest winner leads.
    assert j["highlights"][0]["ticker"] == "SNDK"
    assert j["highlights"][0]["direction"] == "win"
    assert j["highlights"][0]["contract"] == "$1,405 Call"
    assert j["highlights"][0]["returnPct"] == 230.6
    assert any(h["ticker"] == "BE" and h["direction"] == "loss" for h in j["highlights"])


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
    body = "export const weeklyJournals: WeeklyJournal[] = [\n"
    for slug in slugs:
        body += f"  {{ slug: '{slug}' }},\n"
    body += "];\n"
    path = tmp_path / "weeklyJournals.ts"
    path.write_text(body, encoding="utf-8")
    return path


def test_upsert_inserts_new_entry_at_top(tmp_path):
    path = _journal_file(tmp_path, "2026-w32")
    entry = build_weekly_journal([_closed("SNDK", 500, return_pct="230.6")], today=_TODAY)
    entry["slug"] = "2026-w33"

    assert upsert_journal_entry(entry, path) is True
    text = path.read_text(encoding="utf-8")
    open_at = text.index("weeklyJournals: WeeklyJournal[] = [")
    new_at = text.index("slug: \"2026-w33\"")
    old_at = text.index("slug: '2026-w32'")
    assert open_at < new_at < old_at   # newest inserted above the existing entry


def test_upsert_is_idempotent_on_duplicate_slug(tmp_path):
    path = _journal_file(tmp_path, "2026-w33")
    entry = build_weekly_journal([_closed("SNDK", 500, return_pct="230.6")], today=_TODAY)
    entry["slug"] = "2026-w33"

    assert upsert_journal_entry(entry, path) is False
    assert path.read_text(encoding="utf-8").count("2026-w33") == 1

from datetime import date

import analytics.earnings.earnings_planner as planner
from analytics.earnings.iv_crush import IVCrushCandidate
from analytics.earnings.earnings_planner import build_earnings_plan_row
from sheets.writer import EARNINGS_PLAN_HEADERS

def test_build_earnings_plan_row():
    cand = IVCrushCandidate(
        ticker="NVDA",
        earnings_date=date(2026, 9, 15),
        days_left=5,
        price=120.0,
        implied_move_pct=8.5,
        em_lower=110.0,
        em_upper=130.0,
        hist_move_pct=7.2,
        hist_bias="6↓/2↑",
        edge="RICH",
        bias="Bullish",
        strategy="Put Credit Spread (sell put below lower EM)",
        current_iv=0.45,
        iv_percentile=85.5
    )
    
    row = build_earnings_plan_row(cand)
    assert row[0] == "NVDA"
    assert row[1] == "2026-09-15"
    assert row[2] == 5
    assert row[3] == "SELL — rich premium"
    assert row[4] == 8.5
    assert row[5] == 7.2
    assert row[6] == "Bullish"
    assert row[7] == "Put Credit Spread (sell put below lower EM)"
    assert row[8] == 110.0
    assert row[9] == 130.0
    assert row[10] == 85.5

def test_build_earnings_plan_row_empty():
    cand = IVCrushCandidate(
        ticker="CRWD",
        earnings_date=date(2026, 9, 15),
        days_left=5
    )
    row = build_earnings_plan_row(cand)
    assert row[0] == "CRWD"
    assert row[4] == "" # implied move
    assert row[5] == "" # hist move
    assert row[8] == "" # lower bounds
    assert row[10] == "" # ivp


def test_main_writes_header_row_first_and_ensures_tabs(monkeypatch):
    """The planner must create the tab and write column headers above its data.

    Offline: stub out the scan, config, and Sheets client so nothing hits the
    network — we only assert the shape of what would be written.
    """
    calls = {"ensure_tabs": 0}
    written = {}

    class FakeWriter:
        def __init__(self, client):
            pass

        def ensure_tabs(self):
            calls["ensure_tabs"] += 1

        def overwrite_earnings_plan(self, blocks):
            written["blocks"] = blocks

    cand = IVCrushCandidate(
        ticker="NVDA", earnings_date=date(2026, 9, 15), days_left=5,
        implied_move_pct=8.5, edge="RICH", bias="Bullish",
    )
    monkeypatch.setattr(planner, "scan_iv_crush", lambda tickers: [cand])
    monkeypatch.setattr(planner, "get_service_account_info", lambda: {})
    monkeypatch.setattr(planner, "get_spreadsheet_id", lambda: "sheet-id")
    monkeypatch.setattr(planner, "SheetClient", lambda info, sid: object())
    monkeypatch.setattr(planner, "PortfolioWriter", FakeWriter)

    assert planner.main(["NVDA"]) == 0
    assert calls["ensure_tabs"] == 1
    blocks = written["blocks"]
    assert blocks[0] == EARNINGS_PLAN_HEADERS       # header row first
    assert blocks[1][0] == "NVDA"                   # then the data row
    assert len(blocks) == 2

"""Wiring tests for `python -m pancherry_export` run() — the drift note and the
--pr branch. The sheet is a FakeSheetClient and publish_draft_pr is stubbed, so
no network and no real PR."""

from __future__ import annotations

from datetime import date

import pancherry_export.__main__ as cli
from pancherry_export.publish import PRResult
from sheets.writer import STOCKS_HEADERS, OPTIONS_HEADERS, TAB_STOCKS, TAB_OPTIONS
from tests.test_writer import FakeSheetClient

_SUMMARY = [["Total P/L", ""], ["Total Fees", ""]]
_TODAY = date(2026, 8, 15)


def _stock_closed(ticker, total, pl):
    # Date, Broker, Ticker, Action, Qty, Price, Total, Fee, Currency, Status,
    # Realized P/L, Realized P/L (SGD), _dedup_key
    return ["2026-08-13", "Tiger", ticker, "Sell", 10, 10.0, total, 1.0, "USD",
            "Closed", pl, "", f"{ticker}-x"]


def _client():
    c = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])
    c.batch_update_values([
        {"range": f"{TAB_STOCKS}!A1",
         "values": _SUMMARY + [[str(h) for h in STOCKS_HEADERS]] + [
             _stock_closed("GOOG", 110, 10),
             _stock_closed("NVDA", 90, -10),
         ]},
        {"range": f"{TAB_OPTIONS}!A1",
         "values": _SUMMARY + [[str(h) for h in OPTIONS_HEADERS]]},
    ])
    return c


def _client_n(n):
    """A client with ``n`` closed (winning) stock rows in the current week."""
    c = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])
    rows = [_stock_closed(f"T{i}", 110, 10) for i in range(n)]
    c.batch_update_values([
        {"range": f"{TAB_STOCKS}!A1",
         "values": _SUMMARY + [[str(h) for h in STOCKS_HEADERS]] + rows},
        {"range": f"{TAB_OPTIONS}!A1",
         "values": _SUMMARY + [[str(h) for h in OPTIONS_HEADERS]]},
    ])
    return c


def _repo(tmp_path):
    d = tmp_path / "src" / "data"
    d.mkdir(parents=True)
    (d / "openPositions.ts").write_text(
        "export const openPositions: OpenPosition[] = [\n];\n", encoding="utf-8")
    (d / "weeklyJournals.ts").write_text(
        "export const weeklyJournals: WeeklyJournal[] = [\n];\n", encoding="utf-8")
    return tmp_path


def test_pr_flag_calls_publish_and_reports_link(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "publish_draft_pr",
                        lambda files, **kw: PRResult(url="https://gh/pull/9", created=True, committed=2))

    msgs = []
    cli.run(_client(), _repo(tmp_path), today=_TODAY, open_pr=True,
            pr_settings={"token": "t", "repo": "o/r"}, notifier=lambda m: msgs.append(m) or True)

    assert "Draft PR opened" in msgs[0]
    assert "https://gh/pull/9" in msgs[0]


def test_rerun_emits_drift_warning_when_more_trades_close(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(cli, "publish_draft_pr",
                        lambda files, **kw: PRResult(url="https://gh/pull/9", created=False, committed=1))

    # First run inserts the week's draft (2 trades).
    cli.run(_client(), repo, today=_TODAY, notifier=lambda m: True)

    # A later run with more closed trades must warn that the story may be stale.
    c2 = _client()
    c2.batch_update_values([{
        "range": f"{TAB_STOCKS}!A1",
        "values": _SUMMARY + [[str(h) for h in STOCKS_HEADERS]] + [
            _stock_closed("GOOG", 110, 10),
            _stock_closed("NVDA", 90, -10),
            _stock_closed("AVGO", 150, 50),
            _stock_closed("META", 130, 30),
        ],
    }])
    msgs = []
    cli.run(c2, repo, today=_TODAY, notifier=lambda m: msgs.append(m) or True)

    assert "may need a revise" in msgs[0]
    assert "was 2, now 4" in msgs[0]


def test_refresh_skipped_when_trade_count_drops(tmp_path):
    """A glitched/stale read that would lower an existing week's count must never
    overwrite good stats (the serial-date 0-trades incident)."""
    repo = _repo(tmp_path)
    cli.run(_client_n(3), repo, today=_TODAY, notifier=lambda m: True)   # w33 = 3 trades

    msgs = []
    cli.run(_client_n(1), repo, today=_TODAY, notifier=lambda m: msgs.append(m) or True)

    text = (repo / "src" / "data" / "journals" / "2026-w33.ts").read_text(encoding="utf-8")
    assert "trades: 3," in text          # kept — not clobbered to 1
    assert "trades: 1," not in text
    assert "SKIPPED" in msgs[0]

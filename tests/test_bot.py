"""Offline tests for the on-demand Telegram quote bot (``alerting.bot``).

All network is injected: ``transport`` fakes ``getUpdates``, ``reply`` captures
outbound text, and ``quote_builder`` stubs the yfinance-backed analysis.
"""

from __future__ import annotations

from datetime import date

from alerting.bot import (
    _capture_cli,
    _trim_for_telegram,
    format_quote,
    is_help,
    parse_options_command,
    parse_ticker,
    run_bot,
)
from analytics.screening.swing import SwingSetup


# --------------------------------------------------------------------------- #
# parse_ticker
# --------------------------------------------------------------------------- #

def test_parse_ticker_slash_command():
    assert parse_ticker("/quote NVDA") == "NVDA"
    assert parse_ticker("/q nvda") == "NVDA"
    assert parse_ticker("/quicktake tsla") == "TSLA"


def test_parse_ticker_strips_botname():
    assert parse_ticker("/quote@MyPortfolioBot TSLA") == "TSLA"


def test_parse_ticker_bare_symbol():
    assert parse_ticker("NVDA") == "NVDA"
    assert parse_ticker("  pltr  ") == "PLTR"


def test_parse_ticker_rejects_noise():
    assert parse_ticker("") is None
    assert parse_ticker("hello there") is None      # multi-word bare text
    assert parse_ticker("/start") is None           # command without a ticker
    assert parse_ticker("/quote") is None           # missing symbol
    assert parse_ticker("/quote 12345") is None      # not alphabetic
    assert parse_ticker("TOOLONGNAME") is None       # > 5 chars


def test_parse_options_command():
    assert parse_options_command("/directional NVDA") == ("directional", "NVDA")
    assert parse_options_command("/dir nvda") == ("directional", "NVDA")
    assert parse_options_command("/options tsla") == ("directional", "TSLA")
    assert parse_options_command("/midweek spy") == ("midweek", "SPY")
    assert parse_options_command("/mw@MyBot qqq") == ("midweek", "QQQ")


def test_parse_options_command_rejects_noise():
    assert parse_options_command("/directional") is None      # missing symbol
    assert parse_options_command("/dir 12345") is None         # not alphabetic
    assert parse_options_command("/quote NVDA") is None        # not an options cmd
    assert parse_options_command("NVDA") is None               # bare word
    assert parse_options_command("") is None


def test_is_help():
    assert is_help("/start")
    assert is_help("/help")
    assert is_help("/help@MyBot")
    assert not is_help("/quote NVDA")
    assert not is_help("NVDA")


# --------------------------------------------------------------------------- #
# format_quote
# --------------------------------------------------------------------------- #

def _setup(**kw) -> SwingSetup:
    base = dict(
        ticker="NVDA", price=181.5, setup="Breakout", rsi14=62.0,
        pct_vs_ma20=2.1, pct_vs_ma50=8.4, atr_pct=3.2,
        pct_below_52w_high=1.5, days_to_earnings=5, theme="AI/Compute",
        note="within 3% of 52w high",
    )
    base.update(kw)
    return SwingSetup(**base)


def test_format_quote_full():
    msg = format_quote(
        _setup(),
        earnings_date=date(2026, 2, 26),
        days_left=5,
        expected_move_pct=8.4,
    )
    assert "NVDA" in msg
    assert "[AI/Compute]" in msg
    assert "$181.50" in msg
    assert "Breakout" in msg
    assert "RSI 62" in msg
    assert "±8.4% exp move" in msg
    assert "26 Feb" in msg


def test_format_quote_handles_missing_fields():
    # No-data-ish setup: no technicals, no earnings — must not raise.
    s = SwingSetup(ticker="XYZ", price=0.0, setup="No-data", theme="Other")
    msg = format_quote(s)
    assert "XYZ" in msg
    assert "n/a" in msg


def test_format_quote_earnings_without_expected_move():
    msg = format_quote(_setup(), earnings_date=date(2026, 2, 26), days_left=3)
    assert "in 3d" in msg
    assert "exp move" not in msg


# --------------------------------------------------------------------------- #
# run_bot (poll loop, fully injected)
# --------------------------------------------------------------------------- #

def _one_update(text: str, chat_id: int = 123, update_id: int = 1) -> dict:
    return {"ok": True, "result": [
        {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}
    ]}


def test_run_bot_replies_to_quote():
    sent: list[tuple[str, str]] = []
    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("/quote NVDA"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        quote_builder=lambda ticker, today=None: f"QUOTE {ticker}",
        once=True,
    )
    assert sent == [("123", "QUOTE NVDA")]


def test_run_bot_ignores_non_ticker_text():
    sent: list[tuple[str, str]] = []
    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("just chatting here"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        quote_builder=lambda ticker, today=None: "should not be called",
        once=True,
    )
    assert sent == []


def test_run_bot_replies_usage_on_help():
    sent: list[tuple[str, str]] = []
    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("/start"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        quote_builder=lambda ticker, today=None: "unused",
        once=True,
    )
    assert len(sent) == 1
    assert "quick-take" in sent[0][1]


def test_run_bot_survives_builder_error():
    sent: list[tuple[str, str]] = []

    def boom(ticker, today=None):
        raise RuntimeError("yfinance down")

    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("/quote NVDA"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        quote_builder=boom,
        once=True,
    )
    assert len(sent) == 1
    assert "Couldn't build a quote for NVDA" in sent[0][1]


def test_run_bot_routes_options_command():
    sent: list[tuple[str, str]] = []
    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("/directional NVDA"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        option_builders={"directional": lambda t, today=None: f"DIR {t}"},
        once=True,
    )
    assert sent == [("123", "DIR NVDA")]


def test_run_bot_survives_options_builder_error():
    sent: list[tuple[str, str]] = []

    def boom(ticker, today=None):
        raise RuntimeError("yfinance down")

    run_bot(
        token="T",
        transport=lambda url, timeout: _one_update("/midweek SPY"),
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        option_builders={"midweek": boom},
        once=True,
    )
    assert len(sent) == 1
    assert "Couldn't build midweek for SPY" in sent[0][1]


def test_capture_cli_and_trim():
    def fake_main(argv):
        print(f"scan {argv[0]}")
        return 0

    assert "scan NVDA" in _capture_cli(fake_main, "NVDA")
    assert _trim_for_telegram("  hi  ") == "hi"
    assert _trim_for_telegram("x" * 5000).endswith("(truncated)")


def test_run_bot_survives_transport_error():
    sent: list[tuple[str, str]] = []

    def bad_transport(url, timeout):
        raise ConnectionError("network down")

    # once=True + transport error must return cleanly without replying.
    run_bot(
        token="T",
        transport=bad_transport,
        reply=lambda chat_id, text: sent.append((chat_id, text)),
        quote_builder=lambda ticker, today=None: "unused",
        once=True,
    )
    assert sent == []

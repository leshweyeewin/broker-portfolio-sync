"""Tests for lemon8 module (step 10 — BUILD_SPEC.md §11b).

Coverage:
- reader.py: read_closed_positions parses stock and option rows from FakeSheetClient
- reader.py: missing expected headers raises SheetReadError (fail loud)
- reader.py: return_pct derived correctly on long closes
- card.py: render_card_summary and render_card_svg
- journal.py: format_caption, format_blog_draft, generate_journal_package
- LOAD-BEARING PRIVACY REQUIREMENT: show_dollar_amounts=False (default) NEVER prints
  absolute $ / currency amounts in rendered output.
"""

from __future__ import annotations

from decimal import Decimal
import pytest

from lemon8.reader import (
    ClosedPosition,
    SheetReadError,
    read_closed_positions,
    _return_pct,
    _dec,
)
from lemon8.card import render_card_summary, render_card_svg
from lemon8.journal import (
    format_caption,
    format_blog_draft,
    generate_journal_package,
)
from sheets.writer import (
    STOCKS_HEADERS,
    OPTIONS_HEADERS,
    TAB_STOCKS,
    TAB_OPTIONS,
)
from tests.test_writer import FakeSheetClient


# Stocks/Options tabs carry a 2-row summary block above the header row (row 3),
# exactly as the writer lays them out.
_SUMMARY_BLOCK = [["Total P/L", ""], ["Total Fees", ""]]


def _setup_sheet_with_closed_rows() -> FakeSheetClient:
    client = FakeSheetClient([TAB_STOCKS, TAB_OPTIONS])

    # Stock rows (header on row 4, data from row 5)
    stocks = _SUMMARY_BLOCK + [
        [str(h) for h in STOCKS_HEADERS],
        ["2025-03-14", "Tiger", "AAPL", "BUY", 10, 150.0, -1500.0, 1.5, "USD", "Open", "", "", "key1"],
        ["2025-03-15", "Tiger", "AAPL", "SELL", 10, 180.0, 1800.0, 1.5, "USD", "Closed", 300.0, 405.0, "key2"],
    ]
    client.batch_update_values([{"range": f"{TAB_STOCKS}!A1", "values": stocks}])

    # Option rows — no Direction column (Date, Broker, Strategy, Stock, Type,
    # Strike, Qty, Expiry, Action, Premium, Total, Fee, Currency, Status, P/L,
    # P/L (SGD), _dedup_key)
    options = _SUMMARY_BLOCK + [
        [str(h) for h in OPTIONS_HEADERS],
        ["2025-03-14", "Tiger", "Short Put", "TSLA", "PUT", 200.0, 1, "2025-04-18", "SELL", 5.0, 500.0, 1.0, "USD", "Open", "", "", "key3"],
        ["2025-03-20", "Tiger", "Short Put", "TSLA", "PUT", 200.0, 1, "2025-04-18", "BUY", 1.0, -100.0, 1.0, "USD", "Closed", 400.0, 540.0, "key4"],
    ]
    client.batch_update_values([{"range": f"{TAB_OPTIONS}!A1", "values": options}])

    return client


# --------------------------------------------------------------------------- #
# reader.py tests
# --------------------------------------------------------------------------- #

def test_read_closed_positions():
    client = _setup_sheet_with_closed_rows()
    closed = read_closed_positions(client)

    assert len(closed) == 2

    # Stock closed
    stock_pos = next(p for p in closed if p.asset == "stock")
    assert stock_pos.symbol == "AAPL"
    assert stock_pos.realized_pl == Decimal("300.0")
    assert stock_pos.realized_pl_sgd == Decimal("405.0")
    assert stock_pos.is_win is True
    assert stock_pos.label == "AAPL"
    # return_pct = 300 / (1800 - 300) * 100 = 20%
    assert stock_pos.return_pct == pytest.approx(Decimal("20.0"))

    # Option closed
    opt_pos = next(p for p in closed if p.asset == "option")
    assert opt_pos.symbol == "TSLA"
    assert opt_pos.strike == "200.0"
    assert opt_pos.option_type == "PUT"
    assert opt_pos.label == "TSLA 200.0 PUT"
    assert opt_pos.is_win is True


def test_dec_parses_currency_formatted_cells():
    # The sheet returns money FORMATTED as currency; _dec must strip $ / commas
    # / accounting negatives or every real P/L silently becomes None.
    assert _dec("$1,234.56") == Decimal("1234.56")
    assert _dec("-$500.00") == Decimal("-500.00")
    assert _dec("($250.00)") == Decimal("-250.00")
    assert _dec("$0.00") == Decimal("0")
    assert _dec("42") == Decimal("42")
    assert _dec("") is None
    assert _dec(None) is None
    assert _dec("n/a") is None


def test_read_closed_positions_missing_header_raises():
    client = FakeSheetClient([TAB_STOCKS])
    # Header on row 4 (after the summary block) but missing Realized P/L etc.
    rows = _SUMMARY_BLOCK + [["Broker", "Ticker", "Status"]]
    client.batch_update_values([{"range": f"{TAB_STOCKS}!A1", "values": rows}])

    with pytest.raises(SheetReadError) as exc_info:
        read_closed_positions(client)
    assert "missing expected column" in str(exc_info.value)


def test_return_pct_calculation():
    # Long close: total proceeds 1800, P/L 300 -> cost = 1500 -> return 20%
    pct = _return_pct(Decimal("1800"), Decimal("300"))
    assert pct == pytest.approx(Decimal("20"))

    # None inputs
    assert _return_pct(None, Decimal("100")) is None
    assert _return_pct(Decimal("1000"), None) is None


# --------------------------------------------------------------------------- #
# PRIVACY TESTS (LOAD-BEARING)
# --------------------------------------------------------------------------- #

def test_privacy_default_hides_dollar_amounts():
    pos = ClosedPosition(
        broker="Tiger",
        symbol="AAPL",
        asset="stock",
        close_date="2025-03-15",
        currency="USD",
        realized_pl=Decimal("300.00"),
        realized_pl_sgd=Decimal("405.00"),
        return_pct=Decimal("20.0"),
    )

    # 1. Caption
    caption = format_caption(pos, "Good earnings play")
    assert "+20.0%" in caption
    assert "300.00" not in caption
    assert "405.00" not in caption
    assert "USD" not in caption

    # 2. Blog draft
    blog = format_blog_draft(pos, "Good earnings play")
    assert "+20.0%" in blog
    assert "300.00" not in blog
    assert "405.00" not in blog

    # 3. Card summary text
    card = render_card_summary(pos)
    assert "+20.0%" in card
    assert "300.00" not in card

    # 4. SVG card
    svg = render_card_svg(pos)
    assert "+20.0%" in svg
    assert "P/L:" not in svg
    assert "300.00" not in svg

    # 5. Full package
    pkg = generate_journal_package(pos, "Good earnings play")
    assert pkg.show_dollar_amounts is False
    assert "300.00" not in pkg.caption
    assert "300.00" not in pkg.blog_draft
    assert "300.00" not in pkg.card_summary


def test_show_dollar_amounts_opt_in():
    pos = ClosedPosition(
        broker="Tiger",
        symbol="AAPL",
        asset="stock",
        close_date="2025-03-15",
        currency="USD",
        realized_pl=Decimal("300.00"),
        realized_pl_sgd=Decimal("405.00"),
        return_pct=Decimal("20.0"),
    )

    caption = format_caption(pos, "Good earnings play", show_dollar_amounts=True)
    assert "+300.00 USD" in caption

    blog = format_blog_draft(pos, "Good earnings play", show_dollar_amounts=True)
    assert "+300.00 USD" in blog

    card = render_card_summary(pos, show_dollar_amounts=True)
    assert "+300.00 USD" in card

    svg = render_card_svg(pos, show_dollar_amounts=True)
    assert "P/L: +300.00 USD" in svg

    pkg = generate_journal_package(pos, "Good earnings play", show_dollar_amounts=True)
    assert pkg.show_dollar_amounts is True
    assert "+300.00 USD" in pkg.caption

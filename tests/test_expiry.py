"""Tests for alerting/expiry.py — the weekly options-expiry Telegram alert.

Fully offline: the sheet is a FakeSheetClient and delivery goes through an
injected notifier, so nothing touches the network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from alerting.expiry import (
    ExpiryReadError,
    ExpiringOption,
    find_expiring_options,
    format_expiry_message,
    run_expiry_alert,
)
from sheets.writer import OPTIONS_HEADERS, TAB_OPTIONS
from tests.test_writer import FakeSheetClient

# Stocks/Options carry a 2-row summary block above the header row (row 3).
_SUMMARY_BLOCK = [["Total P/L", ""], ["Total Fees", ""]]

# A Sunday, matching the intended run cadence. Window is 7 days → horizon Aug 24.
_TODAY = date(2026, 8, 17)

# OPTIONS_HEADERS order: Date, Broker, Strategy, Stock, Type, Strike, Qty,
# Expiry, Action, Premium, Total, Fee, Currency, Status, P/L, P/L (SGD), _dedup_key
def _opt(broker, stock, otype, strike, qty, expiry, action, status="Open"):
    return [
        "2026-08-11", broker, f"{action} {otype}", stock, otype, strike, qty,
        expiry, action, 1.0, 100.0, 1.0, "USD", status, "", "", f"{stock}-{action}-{expiry}",
    ]


def _client_with(rows) -> FakeSheetClient:
    client = FakeSheetClient([TAB_OPTIONS])
    data = _SUMMARY_BLOCK + [[str(h) for h in OPTIONS_HEADERS]] + rows
    client.batch_update_values([{"range": f"{TAB_OPTIONS}!A1", "values": data}])
    return client


# --------------------------------------------------------------------------- #
# find_expiring_options
# --------------------------------------------------------------------------- #

def test_finds_open_contracts_in_window_and_nets_quantity():
    client = _client_with([
        _opt("Tiger", "NVDA", "Call", "$130.00", 2, "2026-08-22", "Buy"),     # long 2, in window
        _opt("Tiger", "PLTR", "Put", "$40.00", 1, "2026-08-22", "Sell"),      # short 1, in window
        _opt("Longbridge", "PYPL", "Call", "$60.00", 1, "2026-08-19", "Opening Balance"),  # long 1
    ])

    out = find_expiring_options(client, today=_TODAY, within_days=7)

    # Sorted by expiry, then underlying: PYPL (19th), NVDA (22nd), PLTR (22nd).
    assert [(o.underlying, o.net_qty, o.side) for o in out] == [
        ("PYPL", Decimal(1), "long"),
        ("NVDA", Decimal(2), "long"),
        ("PLTR", Decimal(-1), "short"),
    ]
    assert out[1].label == "NVDA 130.00 Call"  # "$" stripped for display


def test_fully_closed_contract_is_excluded():
    # Bought 1 and sold 1 of the same contract in-window → net 0 → not reported.
    client = _client_with([
        _opt("Longbridge", "TEAM", "Call", "$300.00", 1, "2026-08-20", "Buy"),
        _opt("Longbridge", "TEAM", "Call", "$300.00", 1, "2026-08-20", "Sell", status="Closed"),
    ])
    assert find_expiring_options(client, today=_TODAY, within_days=7) == []


def test_expiry_outside_window_is_excluded():
    client = _client_with([
        _opt("Tiger", "AAPL", "Call", "$250.00", 1, "2026-09-05", "Buy"),   # too far
        _opt("Tiger", "MSFT", "Put", "$400.00", 1, "2026-08-10", "Buy"),    # already past
    ])
    assert find_expiring_options(client, today=_TODAY, within_days=7) == []


def test_boundary_days_are_inclusive():
    client = _client_with([
        _opt("Tiger", "TODAYCO", "Call", "$1.00", 1, "2026-08-17", "Buy"),  # == today
        _opt("Tiger", "EDGECO", "Call", "$1.00", 1, "2026-08-24", "Buy"),   # == today + 7
    ])
    underlyings = {o.underlying for o in find_expiring_options(client, today=_TODAY, within_days=7)}
    assert underlyings == {"TODAYCO", "EDGECO"}


def test_missing_header_fails_loud():
    client = FakeSheetClient([TAB_OPTIONS])
    bad_header = [h for h in OPTIONS_HEADERS if h != "Expiry"]  # drop a needed column
    data = _SUMMARY_BLOCK + [[str(h) for h in bad_header]]
    client.batch_update_values([{"range": f"{TAB_OPTIONS}!A1", "values": data}])
    with pytest.raises(ExpiryReadError):
        find_expiring_options(client, today=_TODAY, within_days=7)


# --------------------------------------------------------------------------- #
# format_expiry_message
# --------------------------------------------------------------------------- #

def test_message_groups_by_expiry_and_flags_side():
    options = [
        ExpiringOption("Longbridge", "PYPL", "Call", "$60.00", date(2026, 8, 19), Decimal(1)),
        ExpiringOption("Tiger", "PLTR", "Put", "$40.00", date(2026, 8, 22), Decimal(-1)),
    ]
    msg = format_expiry_message(options, today=_TODAY, within_days=7)
    assert "PYPL 60.00 Call" in msg
    assert "×1 (long)" in msg
    assert "×1 (short)" in msg
    assert "Wed 19 Aug" in msg and "Sat 22 Aug" in msg


def test_empty_message_states_nothing_expiring():
    msg = format_expiry_message([], today=_TODAY, within_days=7)
    assert "No open options expiring" in msg


# --------------------------------------------------------------------------- #
# run_expiry_alert
# --------------------------------------------------------------------------- #

def test_run_sends_formatted_message_and_reports_delivery():
    client = _client_with([
        _opt("Tiger", "NVDA", "Call", "$130.00", 2, "2026-08-22", "Buy"),
    ])
    sent: list[str] = []

    def notifier(text: str) -> bool:
        sent.append(text)
        return True

    options, delivered = run_expiry_alert(client, notifier=notifier, today=_TODAY)

    assert delivered is True
    assert len(options) == 1 and options[0].underlying == "NVDA"
    assert len(sent) == 1 and "NVDA" in sent[0]


def test_run_reports_failed_delivery():
    client = _client_with([])
    options, delivered = run_expiry_alert(client, notifier=lambda _t: False, today=_TODAY)
    assert options == [] and delivered is False

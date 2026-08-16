"""Read closed positions from the portfolio Sheet (read-only).

The sync pipeline (steps 5/9) marks a stock/option execution ``Closed`` and
joins its realized P/L onto the row. This module reads those rows back and
derives the **return %** for each — the only figure the journal needs by
default. Dollars are read too, but the renderer hides them unless explicitly
asked (see ``lemon8/journal.py``).

Columns are located by *header name* (read from the sheet's own header row), so
a column reorder in the writer doesn't silently corrupt the mapping — a missing
expected column fails loud instead (§9).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sheets.writer import (
    STOCKS_HEADERS,
    OPTIONS_HEADERS,
    TAB_STOCKS,
    TAB_OPTIONS,
    DATA_HEADER_ROWS,
    _col_letter,
    sheet_date_to_iso,
)

CLOSED_STATUS = "Closed"


class SheetReadError(RuntimeError):
    """Raised when the sheet's shape doesn't match what we expect (§9)."""


@dataclass(frozen=True)
class ClosedPosition:
    """One closed trade, as read back from the sheet.

    ``return_pct`` is the realized P/L as a percentage of the capital that was
    at risk. It is ``None`` when it can't be derived reliably from a single
    closing row (e.g. short closes, where the opening proceeds aren't on the
    row) — callers must handle that rather than invent a number.
    """

    broker: str
    symbol: str                       # ticker (stock) or underlying (option)
    asset: str                        # "stock" | "option"
    close_date: str                   # ISO date string, as stored
    currency: str
    realized_pl: Optional[Decimal]        # native currency
    realized_pl_sgd: Optional[Decimal]
    return_pct: Optional[Decimal]
    # option-only extras (empty strings for stocks)
    option_type: str = ""
    strike: str = ""
    expiry: str = ""
    strategy: str = ""                    # option Strategy cell; "Stock" for stocks

    @property
    def is_win(self) -> Optional[bool]:
        if self.realized_pl is None:
            return None
        return self.realized_pl > 0

    @property
    def label(self) -> str:
        """Human label for the instrument, e.g. ``AAPL`` or ``SPY 400 Put``."""
        if self.asset == "option":
            parts = [self.symbol, self.strike, self.option_type]
            return " ".join(p for p in parts if p)
        return self.symbol


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def read_closed_positions(client) -> list[ClosedPosition]:
    """Read every ``Closed`` stock and option row from the sheet.

    ``client`` is a ``sheets.writer.SheetClient`` (or any object with a
    ``get_values(range_) -> list[list]`` method — a fake in tests).
    """
    out: list[ClosedPosition] = []
    out += _read_tab(client, TAB_STOCKS, STOCKS_HEADERS, asset="stock")
    out += _read_tab(client, TAB_OPTIONS, OPTIONS_HEADERS, asset="option")
    return out


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _read_tab(client, tab: str, headers: list[str], *, asset: str) -> list[ClosedPosition]:
    col_letter = _col_letter(len(headers))
    values = client.get_values(f"{tab}!A:{col_letter}")

    # Stocks/Options carry a summary block above the column headers, so the
    # header isn't row 0 — it's on the row the writer records in DATA_HEADER_ROWS.
    header_idx = DATA_HEADER_ROWS.get(tab, 1) - 1
    if len(values) <= header_idx:
        return []

    idx = _index_map(tab, values[header_idx])
    positions: list[ClosedPosition] = []
    for row in values[header_idx + 1:]:
        if _cell(row, idx["Status"]) != CLOSED_STATUS:
            continue
        positions.append(_build_position(row, idx, asset=asset))
    return positions


def _index_map(tab: str, header_row: list[Any]) -> dict[str, int]:
    """Map header name -> column index from the sheet's own header row.

    Fails loud if the columns the reader depends on aren't present, rather than
    guessing positions (§9).
    """
    present = {str(name).strip(): i for i, name in enumerate(header_row)}
    needed_stock = {"Broker", "Ticker", "Date", "Currency", "Status",
                    "Total", "Realized P/L", "Realized P/L (SGD)"}
    needed_option = {"Broker", "Stock", "Type", "Strike", "Expiry", "Date",
                     "Currency", "Status", "Total", "P/L", "P/L (SGD)"}
    needed = needed_option if tab == TAB_OPTIONS else needed_stock
    missing = needed - present.keys()
    if missing:
        raise SheetReadError(
            f"{tab} tab is missing expected column(s): {sorted(missing)}. "
            f"Found: {sorted(present.keys())}"
        )
    return present


def _build_position(row: list[Any], idx: dict[str, int], *, asset: str) -> ClosedPosition:
    if asset == "option":
        symbol = str(_cell(row, idx["Stock"]))
        pl = _dec(_cell(row, idx["P/L"]))
        pl_sgd = _dec(_cell(row, idx["P/L (SGD)"]))
        option_type = str(_cell(row, idx["Type"]))
        strike = str(_cell(row, idx["Strike"]))
        expiry = sheet_date_to_iso(_cell(row, idx["Expiry"]))
        # Strategy is always in OPTIONS_HEADERS, but read defensively so a future
        # column drop degrades to an empty label rather than a KeyError.
        strategy = str(_cell(row, idx["Strategy"])) if "Strategy" in idx else ""
    else:
        symbol = str(_cell(row, idx["Ticker"]))
        pl = _dec(_cell(row, idx["Realized P/L"]))
        pl_sgd = _dec(_cell(row, idx["Realized P/L (SGD)"]))
        option_type = strike = expiry = ""
        strategy = "Stock"

    total = _dec(_cell(row, idx["Total"]))
    return ClosedPosition(
        broker=str(_cell(row, idx["Broker"])),
        symbol=symbol,
        asset=asset,
        close_date=sheet_date_to_iso(_cell(row, idx["Date"])),
        currency=str(_cell(row, idx["Currency"])),
        realized_pl=pl,
        realized_pl_sgd=pl_sgd,
        return_pct=_return_pct(total, pl),
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        strategy=strategy,
    )



def _return_pct(total: Optional[Decimal], realized_pl: Optional[Decimal]) -> Optional[Decimal]:
    """Realized P/L as a % of the capital at risk on a *long* close.

    The single trustworthy case is a **long position closed by a SALE**: that
    row's ``Total`` is the (positive) sale proceeds, so ``cost = proceeds - P/L``
    is the real cost basis and ``return = P/L / cost``.

    Any other closing row can't yield a % from one row, so we return ``None``:
    - ``Total``/``P/L`` missing.
    - ``Total <= 0`` — a BUY-to-close (short cover / short-option buyback). Here
      ``Total`` is the buyback *outflow*, not the cost basis; the opening premium
      that WAS the capital at risk isn't on this row. (Using ``abs(Total)`` here
      was the old bug: it manufactured a denominator and printed nonsense like a
      short put buyback showing +4058%.)
    - Derived ``cost <= 0``.

    Callers must not fabricate a percentage when this returns ``None``.
    """
    if total is None or realized_pl is None:
        return None
    if total <= 0:                       # buy-to-close: cost basis not on this row
        return None
    cost = total - realized_pl
    if cost <= 0:
        return None
    return (realized_pl / cost) * Decimal(100)


def _cell(row: list[Any], i: int) -> Any:
    """Value at column ``i``; '' if the row is short (Sheets omits trailing empties)."""
    return row[i] if i < len(row) else ""


def _dec(val: Any) -> Optional[Decimal]:
    """Coerce a sheet cell to Decimal; '' / None -> None (not an error).

    Money cells are read back FORMATTED (the sheet formats them as currency), so
    a P/L arrives as ``"$1,234.56"`` or ``"-$500.00"`` — strip the currency
    symbol, thousands separators, and any parentheses-negative before parsing, or
    every real P/L would silently become ``None``.
    """
    if val is None or val == "":
        return None
    s = str(val).strip()
    neg = s.startswith("(") and s.endswith(")")   # accounting-style negative
    s = s.strip("()").replace(",", "").replace("$", "").replace("−", "-").strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return -d if neg else d

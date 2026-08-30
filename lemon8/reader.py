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
import re
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
    action: str = ""                      # Buy / Sell (the closing execution's action)
    reason: str = ""                      # manual free-text thesis from the sheet's Reason cell

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

    @property
    def kind(self) -> str:
        """Short 'what kind of trade' label — the closest thing to a *why* the
        sheet carries: an option's Strategy (e.g. ``Cash Secured Put``), falling
        back to Call/Put; a stock's closing Action (Buy/Sell). ``""`` if neither
        is recorded."""
        if self.asset == "option":
            return (self.strategy or self.option_type).strip()
        return self.action.strip()


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
    for r_i, row in enumerate(values[header_idx + 1:], start=header_idx + 1):
        if _cell(row, idx["Status"]) != CLOSED_STATUS:
            continue
        positions.append(_build_position(row, r_i, values, header_idx, idx, asset=asset))
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


def _build_position(
    row: list[Any],
    row_idx: int,
    values: list[list[Any]],
    header_idx: int,
    idx: dict[str, int],
    *,
    asset: str,
) -> ClosedPosition:
    if asset == "option":
        symbol = str(_cell(row, idx["Stock"]))
        pl = _evaluate_pl_cell(_cell(row, idx["P/L"]), values, header_idx)
        pl_sgd = _dec(_cell(row, idx["P/L (SGD)"]))
        option_type = str(_cell(row, idx["Type"]))
        strike = str(_cell(row, idx["Strike"]))
        expiry = sheet_date_to_iso(_cell(row, idx["Expiry"]))
        strategy = str(_cell(row, idx["Strategy"])) if "Strategy" in idx else ""
    else:
        symbol = str(_cell(row, idx["Ticker"]))
        pl = _evaluate_pl_cell(_cell(row, idx["Realized P/L"]), values, header_idx)
        pl_sgd = _dec(_cell(row, idx["Realized P/L (SGD)"]))
        option_type = strike = expiry = ""
        strategy = "Stock"

    # The closing execution's Buy/Sell (read defensively — not in the required set).
    action = str(_cell(row, idx["Action"])) if "Action" in idx else ""
    reason = str(_cell(row, idx["Reason"])).strip() if "Reason" in idx else ""
    cost_basis = _find_cost_basis(row, row_idx, values, header_idx, idx, asset=asset)
    return ClosedPosition(
        broker=str(_cell(row, idx["Broker"])),
        symbol=symbol,
        asset=asset,
        close_date=sheet_date_to_iso(_cell(row, idx["Date"])),
        currency=str(_cell(row, idx["Currency"])),
        realized_pl=pl,
        realized_pl_sgd=pl_sgd,
        return_pct=_return_pct(cost_basis, pl),
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        strategy=strategy,
        action=action,
        reason=reason,
    )


def _evaluate_pl_cell(raw_pl: Any, values: list[list[Any]], header_idx: int) -> Optional[Decimal]:
    """Coerce P/L to Decimal; if a formula like `=K327+K207-L207-L327`, evaluate referenced cells."""
    if raw_pl is None or raw_pl == "":
        return None
    s = str(raw_pl).strip()
    if not s.startswith("="):
        return _dec(raw_pl)
    
    expr = s[1:].strip()
    terms = re.findall(r"([+-]?)\s*([A-Za-z]+)(\d+)", expr)
    if not terms:
        return _dec(raw_pl)
    
    total = Decimal(0)
    for sign, col_str, row_str in terms:
        col_idx = 0
        for c in col_str.upper():
            col_idx = col_idx * 26 + (ord(c) - ord("A") + 1)
        col_idx -= 1
        r_num = int(row_str)
        if 1 <= r_num <= len(values):
            val = _dec(_cell(values[r_num - 1], col_idx)) or Decimal(0)
            total += -val if sign == "-" else val
    return total


def _find_cost_basis(
    row: list[Any],
    row_idx: int,
    values: list[list[Any]],
    header_idx: int,
    idx: dict[str, int],
    *,
    asset: str,
) -> Optional[Decimal]:
    """Find the initial capital at risk (cost/premium basis) for a closed position.

    1. Formula check: If P/L formula references an opening row (e.g. K207), use its Total.
    2. Backward match: Look back for the matching opening transaction of the same instrument.
    3. Fallback: Use current row Total if non-zero.
    """
    pl_col = idx.get("P/L" if asset == "option" else "Realized P/L", 0)
    raw_pl = _cell(row, pl_col)

    # 1. Formula reference
    if str(raw_pl).startswith("="):
        ref_rows = [int(m[1]) for m in re.findall(r"([A-Za-z]+)(\d+)", str(raw_pl))]
        open_rows = [r for r in ref_rows if r != (row_idx + 1) and 1 <= r <= len(values)]
        if open_rows:
            open_tot = _dec(_cell(values[open_rows[0] - 1], idx.get("Total", 0)))
            if open_tot and open_tot != 0:
                return abs(open_tot)

    stock_col = idx.get("Stock" if asset == "option" else "Ticker")
    symbol = str(_cell(row, stock_col)).strip()
    action = str(_cell(row, idx.get("Action", 0))).strip().lower()

    # 2. Backward search for matching opening trade
    if asset == "option":
        opt_type = str(_cell(row, idx.get("Type", 0))).strip().lower()
        strike = str(_cell(row, idx.get("Strike", 0))).strip()
        expiry = str(_cell(row, idx.get("Expiry", 0))).strip()
        open_action = "sell" if action.startswith("b") else "buy"

        for r_i in range(row_idx - 1, header_idx, -1):
            r = values[r_i]
            if str(_cell(r, stock_col)).strip() == symbol:
                r_typ = str(_cell(r, idx.get("Type", 0))).strip().lower()
                r_stk = str(_cell(r, idx.get("Strike", 0))).strip()
                r_exp = str(_cell(r, idx.get("Expiry", 0))).strip()
                r_act = str(_cell(r, idx.get("Action", 0))).strip().lower()
                if r_typ == opt_type and r_stk == strike and r_exp == expiry:
                    if r_act.startswith(open_action[:1]):
                        tot = _dec(_cell(r, idx.get("Total", 0)))
                        if tot and tot != 0:
                            return abs(tot)
    else:
        open_action = "buy" if action.startswith("s") else "sell"
        for r_i in range(row_idx - 1, header_idx, -1):
            r = values[r_i]
            if str(_cell(r, stock_col)).strip() == symbol:
                r_act = str(_cell(r, idx.get("Action", 0))).strip().lower()
                if r_act.startswith(open_action[:1]):
                    tot = _dec(_cell(r, idx.get("Total", 0)))
                    if tot and tot != 0:
                        return abs(tot)

    # 3. Fallback to current row total
    cur_tot = _dec(_cell(row, idx.get("Total", 0)))
    if cur_tot and cur_tot != 0:
        return abs(cur_tot)
    return None


def _return_pct(cost_basis: Optional[Decimal], realized_pl: Optional[Decimal]) -> Optional[Decimal]:
    """Realized P/L as a % of the initial capital at risk."""
    if cost_basis is None or realized_pl is None or cost_basis == 0:
        return None
    return (realized_pl / abs(cost_basis)) * Decimal(100)


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

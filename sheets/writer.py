"""Google Sheets idempotent writer (step 5 — BUILD_SPEC.md §4, §6, §12).

Architecture
------------
* **`SheetClient`** — thin wrapper around the Sheets v4 API. Owns auth and all
  raw batchGet / batchUpdate calls. Stateless between calls.

* **`PortfolioWriter`** — business logic. Knows the tab/column layout, the
  upsert logic, and summary-block placement. Calls ``SheetClient`` for all I/O.
  This separation keeps the business logic fully testable offline (mock
  SheetClient in tests).

Idempotency guarantee (§6)
--------------------------
Every execution/movement row has a ``_dedup_key``. The writer:
  1. Reads the current ``_dedup_key`` column from the sheet.
  2. Partitions incoming rows into *updates* (key already in sheet) and
     *appends* (new key).
  3. Sends a single batchUpdate: cell-level updates for changed rows + one
     append for new rows.
Re-running the job with the same data produces exactly the same sheet state.

Tab layout (§4)
---------------
``Transactions`` tab  — one row per cash movement (deposits/withdrawals only).
``Stocks`` tab        — one row per stock execution + summary block above data.
``Options`` tab       — one row per option execution + summary block above data.
``Run Log`` tab       — one row per pipeline run (appended, never updated).
``Dashboard`` tab     — fully computed; overwritten each run (no dedup needed).

The ``_dedup_key`` column is the **last column** in the data range on each tab
and is hidden via the Sheets API so it is not visible to the user.

Auth
----
The service account JSON is loaded from environment:
  * ``GOOGLE_SERVICE_ACCOUNT_JSON`` — the JSON string (Cloud Run / CI)
  * ``GOOGLE_APPLICATION_CREDENTIALS`` — path to JSON file (local dev)

The Spreadsheet ID comes from ``PORTFOLIO_SPREADSHEET_ID``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants — tab names and column definitions
# --------------------------------------------------------------------------- #

TAB_TRANSACTIONS = "Transactions"
TAB_STOCKS       = "Stocks"
TAB_OPTIONS      = "Options"
TAB_DASHBOARD    = "Dashboard"
TAB_RUN_LOG      = "Run Log"

# Column headers for each data tab. _dedup_key is always last (will be hidden).
TRANSACTIONS_HEADERS = [
    "Date", "Broker", "Type", "Amount", "Currency",
    "Amount (SGD)", "Note", "_dedup_key",
]
STOCKS_HEADERS = [
    "Date", "Broker", "Ticker", "Action", "Qty", "Price", "Total",
    "Fee", "Currency", "Status", "Realized P/L", "Realized P/L (SGD)",
    "_dedup_key",
]
OPTIONS_HEADERS = [
    "Date", "Broker", "Strategy", "Direction", "Stock", "Type", "Strike",
    "Qty", "Expiry", "Action", "Premium", "Total", "Fee", "Currency",
    "Status", "P/L", "P/L (SGD)", "_dedup_key",
]
RUN_LOG_HEADERS = [
    "Timestamp", "Status", "Stocks Added", "Stocks Updated",
    "Options Added", "Options Updated", "Transactions Added",
    "FX Rates Used", "Reconciliation", "Warnings",
]
DASHBOARD_HEADERS = ["Metric", "Longbridge", "Tiger", "MooMoo", "Total (SGD)"]

# How many header rows each data tab has before row data starts.
# 1 = just the column header row.
DATA_HEADER_ROWS = {
    TAB_TRANSACTIONS: 1,
    TAB_STOCKS: 1,
    TAB_OPTIONS: 1,
    TAB_RUN_LOG: 1,
}

ALL_TABS = [TAB_TRANSACTIONS, TAB_STOCKS, TAB_OPTIONS, TAB_DASHBOARD, TAB_RUN_LOG]

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# --------------------------------------------------------------------------- #
# Row value helpers
# --------------------------------------------------------------------------- #

def _fmt(val: object) -> str | int | float:
    """Format a Python value into a Sheets-friendly scalar."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return val
    if isinstance(val, Decimal):
        return float(val)  # Sheets JSON requires JSON-serialisable numbers
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _dedup_col_index(headers: list[str]) -> int:
    """0-based index of the _dedup_key column."""
    return headers.index("_dedup_key")


# --------------------------------------------------------------------------- #
# SheetClient — thin Sheets API wrapper
# --------------------------------------------------------------------------- #

class SheetClient:
    """Thin wrapper around the Sheets v4 REST API.

    Keeps auth and raw API calls separate from business logic so tests can
    replace this with a lightweight fake (see ``FakeSheetClient`` in tests).

    Parameters
    ----------
    service_account_info:
        Dict from the parsed service-account JSON (as returned by
        ``config.settings.get_service_account_info()``).
    spreadsheet_id:
        The Google Sheet ID from the URL (…/d/<ID>/edit).
    """

    def __init__(self, service_account_info: dict, spreadsheet_id: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._service = self._build_service(service_account_info)

    @staticmethod
    def _build_service(info: dict):
        from google.oauth2.service_account import Credentials  # type: ignore
        from googleapiclient.discovery import build              # type: ignore
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ---- reads -------------------------------------------------------

    def get_values(self, range_: str) -> list[list[Any]]:
        """Return 2-D list of cell values for ``range_`` (e.g. ``'Stocks!A:M'``)."""
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=range_)
            .execute()
        )
        return result.get("values", [])

    def get_sheet_metadata(self) -> dict:
        """Return the full spreadsheet metadata (sheet IDs, names, etc.)."""
        return (
            self._service.spreadsheets()
            .get(spreadsheetId=self._spreadsheet_id)
            .execute()
        )

    # ---- writes ------------------------------------------------------

    def batch_update_values(self, data: list[dict]) -> dict:
        """Write a list of ``{range, values}`` dicts in one API call."""
        body = {"valueInputOption": "USER_ENTERED", "data": data}
        return (
            self._service.spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=self._spreadsheet_id, body=body)
            .execute()
        )

    def append_values(self, range_: str, values: list[list[Any]]) -> dict:
        """Append rows below the last row of data in ``range_``."""
        body = {"values": values}
        return (
            self._service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self._spreadsheet_id,
                range=range_,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
            .execute()
        )

    def batch_update(self, requests: list[dict]) -> dict:
        """Send a batchUpdate request (for formatting, hidden columns, etc.)."""
        body = {"requests": requests}
        return (
            self._service.spreadsheets()
            .batchUpdate(spreadsheetId=self._spreadsheet_id, body=body)
            .execute()
        )

    def clear_range(self, range_: str) -> None:
        """Clear all values in the given range (used for Dashboard overwrite)."""
        self._service.spreadsheets().values().clear(
            spreadsheetId=self._spreadsheet_id, range=range_
        ).execute()

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id


# --------------------------------------------------------------------------- #
# Upsert result
# --------------------------------------------------------------------------- #

@dataclass
class UpsertResult:
    """Counts returned by a single tab's upsert for the Run Log."""
    tab: str
    added: int = 0
    updated: int = 0


# --------------------------------------------------------------------------- #
# PortfolioWriter — business logic
# --------------------------------------------------------------------------- #

class PortfolioWriter:
    """Writes pipeline output to the target Google Sheet, idempotently.

    Parameters
    ----------
    client:
        A ``SheetClient`` (or a compatible fake for tests).
    """

    def __init__(self, client: SheetClient) -> None:
        self._client = client
        self._sheet_ids: dict[str, int] = {}  # tab name -> sheet ID (for formatting)

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #

    def ensure_tabs(self) -> None:
        """Create missing tabs and write header rows. Safe to call on every run.

        If a tab already exists (matching by title), it is left untouched.
        New tabs are created and headers are written. The ``_dedup_key`` column
        on data tabs is then hidden so users don't see internal keys.
        """
        meta = self._client.get_sheet_metadata()
        existing = {s["properties"]["title"] for s in meta["sheets"]}
        self._sheet_ids = {
            s["properties"]["title"]: s["properties"]["sheetId"]
            for s in meta["sheets"]
        }

        create_requests = []
        for tab in ALL_TABS:
            if tab not in existing:
                create_requests.append({"addSheet": {"properties": {"title": tab}}})

        if create_requests:
            result = self._client.batch_update(create_requests)
            # Update sheet IDs map with newly created sheets
            for reply in result.get("replies", []):
                if "addSheet" in reply:
                    props = reply["addSheet"]["properties"]
                    self._sheet_ids[props["title"]] = props["sheetId"]

        # Write headers for tabs that were just created or are empty
        self._ensure_headers()

        # Hide _dedup_key column on data tabs
        self._hide_dedup_columns()

    def upsert_transactions(self, rows: list[list[Any]]) -> UpsertResult:
        """Idempotent upsert for the Transactions tab.

        ``rows`` is a list of value lists matching ``TRANSACTIONS_HEADERS``
        (excluding the header row itself — just data rows).
        The last element of each row must be the ``_dedup_key``.
        """
        return self._upsert_tab(TAB_TRANSACTIONS, TRANSACTIONS_HEADERS, rows)

    def upsert_stocks(self, rows: list[list[Any]]) -> UpsertResult:
        """Idempotent upsert for the Stocks tab."""
        return self._upsert_tab(TAB_STOCKS, STOCKS_HEADERS, rows)

    def upsert_options(self, rows: list[list[Any]]) -> UpsertResult:
        """Idempotent upsert for the Options tab."""
        return self._upsert_tab(TAB_OPTIONS, OPTIONS_HEADERS, rows)

    def append_run_log(self, row: list[Any]) -> None:
        """Append one row to the Run Log tab (always append, never update)."""
        self._client.append_values(f"{TAB_RUN_LOG}!A:J", [row])

    def overwrite_dashboard(self, blocks: list[list[Any]]) -> None:
        """Overwrite the Dashboard tab with the given blocks.

        The Dashboard is fully machine-computed each run, so it is cleared
        and rewritten rather than upserted.
        """
        if not blocks:
            return
        self._client.clear_range(f"{TAB_DASHBOARD}!A1:Z1000")
        self._client.batch_update_values([
            {"range": f"{TAB_DASHBOARD}!A1", "values": blocks}
        ])

    # ------------------------------------------------------------------ #
    # Core upsert logic
    # ------------------------------------------------------------------ #

    def _upsert_tab(
        self,
        tab: str,
        headers: list[str],
        incoming_rows: list[list[Any]],
    ) -> UpsertResult:
        """Read existing keys, partition into updates vs appends, apply both.

        Algorithm:
        1. Fetch all rows currently in the sheet (skip the header row).
        2. Build a map: dedup_key -> (row_index in sheet, row_values).
           Row index is 1-based (Sheets rows), with row 1 being the header.
        3. For each incoming row:
           - If key exists: check if values changed; if so, queue an update.
           - If key is new: queue for append.
        4. Send one batchUpdate for all in-place updates.
        5. Send one append call for all new rows.
        """
        result = UpsertResult(tab=tab)
        if not incoming_rows:
            return result

        dedup_col = _dedup_col_index(headers)
        n_cols = len(headers)
        col_letter = _col_letter(n_cols)  # e.g. "M" for 13 columns
        range_ = f"{tab}!A:{col_letter}"

        # 1. Fetch existing data (everything, including header)
        existing_all = self._client.get_values(range_)

        # 2. Build key -> (1-based sheet row number, padded row values)
        header_rows = DATA_HEADER_ROWS.get(tab, 1)
        data_rows = existing_all[header_rows:] if len(existing_all) > header_rows else []
        existing_map: dict[str, tuple[int, list]] = {}
        for offset, row in enumerate(data_rows):
            # Pad row to full width (Sheets omits trailing empty cells)
            padded = row + [""] * (n_cols - len(row))
            key = padded[dedup_col]
            if key:
                sheet_row = header_rows + offset + 1  # 1-based
                existing_map[key] = (sheet_row, padded)

        # 3. Partition
        updates: list[dict] = []  # batchUpdate value data
        appends: list[list[Any]] = []

        for row in incoming_rows:
            padded = list(row) + [""] * (n_cols - len(row))
            key = padded[dedup_col]
            if not key:
                log.warning("%s: skipping row with empty _dedup_key: %s", tab, row)
                continue

            if key in existing_map:
                sheet_row, old_vals = existing_map[key]
                new_formatted = [_fmt(v) for v in padded]
                old_formatted = [_fmt(v) for v in old_vals]
                if new_formatted != old_formatted:
                    updates.append({
                        "range": f"{tab}!A{sheet_row}:{col_letter}{sheet_row}",
                        "values": [new_formatted],
                    })
                    result.updated += 1
            else:
                appends.append([_fmt(v) for v in padded])
                result.added += 1

        # 4. Send updates
        if updates:
            self._client.batch_update_values(updates)

        # 5. Append new rows
        if appends:
            self._client.append_values(range_, appends)

        return result

    # ------------------------------------------------------------------ #
    # Initialisation helpers
    # ------------------------------------------------------------------ #

    def _ensure_headers(self) -> None:
        """Write header rows to tabs that don't have one yet."""
        tab_headers = {
            TAB_TRANSACTIONS: TRANSACTIONS_HEADERS,
            TAB_STOCKS:       STOCKS_HEADERS,
            TAB_OPTIONS:      OPTIONS_HEADERS,
            TAB_RUN_LOG:      RUN_LOG_HEADERS,
            TAB_DASHBOARD:    DASHBOARD_HEADERS,
        }
        write_data = []
        for tab, headers in tab_headers.items():
            col_letter = _col_letter(len(headers))
            existing = self._client.get_values(f"{tab}!A1:{col_letter}1")
            if not existing or not existing[0]:
                write_data.append({
                    "range": f"{tab}!A1",
                    "values": [[str(h) for h in headers]],
                })
        if write_data:
            self._client.batch_update_values(write_data)

    def _hide_dedup_columns(self) -> None:
        """Hide the _dedup_key column on all data tabs."""
        tab_headers = {
            TAB_TRANSACTIONS: TRANSACTIONS_HEADERS,
            TAB_STOCKS:       STOCKS_HEADERS,
            TAB_OPTIONS:      OPTIONS_HEADERS,
        }
        requests = []
        for tab, headers in tab_headers.items():
            sheet_id = self._sheet_ids.get(tab)
            if sheet_id is None:
                continue
            col_idx = _dedup_col_index(headers)  # 0-based
            requests.append({
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            })
        if requests:
            self._client.batch_update(requests)


# --------------------------------------------------------------------------- #
# Column-letter helper
# --------------------------------------------------------------------------- #

def _col_letter(n: int) -> str:
    """Convert 1-based column count to a Sheets column letter (A, B, ..., Z, AA, ...)."""
    if n <= 0:
        raise ValueError(f"column count must be >= 1, got {n}")
    result = ""
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


# --------------------------------------------------------------------------- #
# Row-builder helpers (convert dataclasses -> flat value lists)
# --------------------------------------------------------------------------- #

def build_transaction_row(cm, sgd_amount: Decimal) -> list[Any]:
    """Convert a CashMovement to a Transactions tab row.

    Only Deposit / Withdrawal rows should be passed here (§8: dividends/fees
    are classified separately and do not appear in the Transactions tab).
    """
    return [
        cm.date.isoformat(),
        cm.broker.value,
        cm.type.value,
        _fmt(cm.amount),
        cm.currency,
        _fmt(sgd_amount),
        cm.note,
        cm.dedup_key,
    ]


def build_stock_row(
    trade,
    status: str = "Open",
    realized_pl: Optional[Decimal] = None,
    realized_pl_sgd: Optional[Decimal] = None,
) -> list[Any]:
    """Convert a StockTrade to a Stocks tab row.

    ``realized_pl`` and ``realized_pl_sgd`` are filled in for Sell rows by
    joining FifoResult.realized_by_key onto the trade's dedup_key.
    """
    return [
        trade.date.isoformat(),
        trade.broker.value,
        trade.ticker,
        trade.action.value,
        _fmt(trade.qty),
        _fmt(trade.price),
        _fmt(trade.total),
        _fmt(trade.fee),
        trade.currency,
        status,
        _fmt(realized_pl) if realized_pl is not None else "",
        _fmt(realized_pl_sgd) if realized_pl_sgd is not None else "",
        trade.dedup_key,
    ]


def build_option_row(
    trade,
    status: str = "Open",
    realized_pl: Optional[Decimal] = None,
    realized_pl_sgd: Optional[Decimal] = None,
) -> list[Any]:
    """Convert an OptionTrade to an Options tab row."""
    return [
        trade.date.isoformat(),
        trade.broker.value,
        trade.strategy,
        trade.direction.value if trade.direction else "",
        trade.underlying,
        trade.option_type.value,
        _fmt(trade.strike),
        _fmt(trade.qty),
        trade.expiry.isoformat(),
        trade.action.value,
        _fmt(trade.premium),
        _fmt(trade.total),
        _fmt(trade.fee),
        trade.currency,
        status,
        _fmt(realized_pl) if realized_pl is not None else "",
        _fmt(realized_pl_sgd) if realized_pl_sgd is not None else "",
        trade.dedup_key,
    ]


def build_run_log_row(
    status: str,
    stocks_added: int,
    stocks_updated: int,
    options_added: int,
    options_updated: int,
    transactions_added: int,
    fx_rates_used: str,
    reconciliation: str,
    warnings: str,
) -> list[Any]:
    """Build a Run Log row (one per pipeline run)."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        ts, status,
        stocks_added, stocks_updated,
        options_added, options_updated,
        transactions_added,
        fx_rates_used,
        reconciliation,
        warnings,
    ]

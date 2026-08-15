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
    "Date", "Broker", "Strategy", "Stock", "Type", "Strike",
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
# 1 = just the column header row. The summary block is placed off to the right.
DATA_HEADER_ROWS = {
    TAB_TRANSACTIONS: 1,
    TAB_STOCKS: 3,   # rows 1-2 = Total P/L + Total Fees summary; row 3 = column headers
    TAB_OPTIONS: 3,
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

    def get_values(self, range_: str, value_render_option: str = "FORMATTED_VALUE") -> list[list[Any]]:
        """Return 2-D list of cell values for ``range_`` (e.g. ``'Stocks!A:M'``).

        Uses ``spreadsheets().get()`` with ``userEnteredValue`` field mask to
        read cell contents without triggering expensive server-side formula
        recalculations that cause timeouts on large sheets.
        """
        result = (
            self._service.spreadsheets()
            .get(
                spreadsheetId=self._spreadsheet_id,
                ranges=[range_],
                fields="sheets.data.rowData.values.userEnteredValue",
            )
            .execute()
        )
        rows: list[list[Any]] = []
        for sheet in result.get("sheets", []):
            for data in sheet.get("data", []):
                for row in data.get("rowData", []):
                    row_vals: list[Any] = []
                    for cell in row.get("values", []):
                        u = cell.get("userEnteredValue", {})
                        if "stringValue" in u:
                            row_vals.append(u["stringValue"])
                        elif "numberValue" in u:
                            n = u["numberValue"]
                            row_vals.append(int(n) if isinstance(n, float) and n.is_integer() else n)
                        elif "boolValue" in u:
                            row_vals.append(u["boolValue"])
                        elif "formulaValue" in u:
                            row_vals.append(u["formulaValue"])
                        else:
                            row_vals.append("")
                    while row_vals and row_vals[-1] == "":
                        row_vals.pop()
                    rows.append(row_vals)
        while rows and not rows[-1]:
            rows.pop()
        return rows

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

    def apply_formatting(self) -> None:
        """Re-apply colours/number formats — call AFTER writing data rows.

        Data rows are appended *below* the header, so they inherit the header's
        green fill + white text; re-running the (idempotent) formatting once the
        rows exist resets them to the theme default. Populates sheet IDs if a
        prior ``ensure_tabs`` hasn't already.
        """
        if not self._sheet_ids:
            meta = self._client.get_sheet_metadata()
            self._sheet_ids = {
                s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta["sheets"]
            }
        self._apply_formatting()
        self._write_summary_formulas()

    def _write_summary_formulas(self) -> None:
        """(Re-)assert the Total P/L / Total Fees SUM formulas AFTER data rows
        exist. The append uses INSERT_ROWS, which shifts an ``=SUM(K4:K)`` written
        pre-append down to ``=SUM(K<n+1>:K)`` (summing nothing). Writing it here,
        after the upsert, pins it back to the fixed data range."""
        self._client.batch_update_values([
            {"range": f"{TAB_STOCKS}!A1:B2", "values": [
                ["Total P/L", "=SUM(K4:K)"], ["Total Fees", "=SUM(H4:H)"]]},
            {"range": f"{TAB_OPTIONS}!A1:B2", "values": [
                ["Total P/L", "=SUM(O4:O)"], ["Total Fees", "=SUM(L4:L)"]]},
        ])

    def sort_data_tabs(self) -> None:
        """Sort each data tab's rows by Date (column A) ascending, keeping the
        hide-Closed filter on the Stocks/Options view.

        Order matters: a basic filter that hides rows makes ``sortRange`` reorder
        only the *visible* rows, pinning the hidden ones and jumbling the data. So
        for each tab we clear the filter, sort ALL rows, then re-apply the filter
        (hiding Closed on Stocks/Options). Net effect: fully sorted underlying
        data, Closed rows hidden in the view. Call after writes — the upsert
        appends new rows at the bottom."""
        if not self._sheet_ids:
            self.apply_formatting()  # populates sheet ids as a side effect
        # tab -> (column count, Status column index for the hide-Closed filter)
        tabs = {
            TAB_TRANSACTIONS: (len(TRANSACTIONS_HEADERS), None),
            TAB_STOCKS: (len(STOCKS_HEADERS), 9),
            TAB_OPTIONS: (len(OPTIONS_HEADERS), 13),
        }
        requests = []
        for tab, (ncols, status_col) in tabs.items():
            sheet_id = self._sheet_ids.get(tab)
            if sheet_id is None:
                continue
            header_row_0 = DATA_HEADER_ROWS.get(tab, 1) - 1
            first_data_0 = DATA_HEADER_ROWS.get(tab, 1)
            # 1. Clear the filter so the sort covers every row.
            requests.append({"clearBasicFilter": {"sheetId": sheet_id}})
            # 2. Sort all data rows ascending by date.
            requests.append({"sortRange": {
                "range": {"sheetId": sheet_id, "startRowIndex": first_data_0,
                          "startColumnIndex": 0, "endColumnIndex": ncols},
                "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
            }})
            # 3. Re-apply the filter (dropdowns on the header; hide Closed rows on
            #    Stocks/Options). This only hides in the view — data stays sorted.
            filter_payload = {"range": {"sheetId": sheet_id, "startRowIndex": header_row_0,
                                        "startColumnIndex": 0, "endColumnIndex": ncols}}
            if status_col is not None:
                filter_payload["criteria"] = {str(status_col): {"hiddenValues": ["Closed"]}}
            requests.append({"setBasicFilter": {"filter": filter_payload}})
        if requests:
            self._client.batch_update(requests)

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
        # Currency-format the numeric region (broker columns B..E of the money
        # rows) so values read as clean SGD amounts, not raw 4-dp floats.
        sheet_id = self._sheet_ids.get(TAB_DASHBOARD)
        if sheet_id is None:
            meta = self._client.get_sheet_metadata()
            self._sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                               for s in meta["sheets"]}
            sheet_id = self._sheet_ids.get(TAB_DASHBOARD)
        # Money rows only (Net Capital In, Account Value, Total P/L, and the three
        # realized windows: This Week / This Month / This Year) — rows 2..7.
        # Excludes the integer Open-Positions counts and text rows.
        money_end = min(7, len(blocks))
        if sheet_id is not None and money_end > 1:
            self._client.batch_update([{"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": money_end,
                          "startColumnIndex": 1, "endColumnIndex": len(DASHBOARD_HEADERS)},
                "cell": {"userEnteredFormat": {"numberFormat": {
                    "type": "CURRENCY", "pattern": "#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat",
            }}])

    def read_net_capital_in_by_broker(self) -> dict[str, "Decimal"]:
        """Net external capital per broker = Σ Deposits − Σ Withdrawals, in SGD,
        read from the Transactions tab (which holds full history; a single run only
        fetches a recent window). Used by the Dashboard to compute Total P/L."""
        from decimal import Decimal
        try:
            vals = self._client.get_values(
                f"{TAB_TRANSACTIONS}!A:{_col_letter(len(TRANSACTIONS_HEADERS))}",
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception:  # noqa: BLE001
            log.warning("Could not read Transactions for net capital in", exc_info=True)
            return {}
        idx = {h: i for i, h in enumerate(TRANSACTIONS_HEADERS)}
        bi, ti, si = idx["Broker"], idx["Type"], idx["Amount (SGD)"]
        hr = DATA_HEADER_ROWS.get(TAB_TRANSACTIONS, 1)
        out: dict[str, Decimal] = {}
        for row in vals[hr:] if len(vals) > hr else []:
            if len(row) <= si:
                continue
            typ = str(row[ti]).strip().lower()
            sign = 1 if typ == "deposit" else (-1 if typ == "withdrawal" else 0)
            if not sign:
                continue
            try:
                amt = Decimal(str(row[si]))
            except Exception:  # noqa: BLE001
                continue
            broker = str(row[bi]).strip()
            out[broker] = out.get(broker, Decimal("0")) + sign * amt
        return out

    def read_opening_balances(self):
        """Read persisted Opening Balance rows back as OPENING_BALANCE trades.

        A forward run (no ``--seed``) must feed these into FIFO — otherwise the
        engine sees only the newly-fetched fills and reconciliation flags every
        long-held position as missing (§5/§14 "seed persistence"). Returns
        ``(stock_trades, option_trades)``; empty lists if the tabs don't exist.
        """
        # Imported here to keep the writer's core free of adapter deps.
        from adapters.base import (
            Broker, OptionAction, OptionTrade, OptionType, StockAction, StockTrade,
        )

        stocks: list[StockTrade] = []
        for item in self.read_all_stock_trades():
            if item["trade"].action == StockAction.OPENING_BALANCE:
                stocks.append(item["trade"])

        options: list[OptionTrade] = []
        for item in self.read_all_option_trades():
            if item["trade"].action == OptionAction.OPENING_BALANCE:
                options.append(item["trade"])

        return stocks, options

    def _read_data_rows(self, tab: str, headers: list[str]) -> list[list[Any]]:
        try:
            vals = self._client.get_values(
                f"{tab}!A:{_col_letter(len(headers))}",
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception:  # noqa: BLE001
            log.warning("Could not read %s", tab, exc_info=True)
            return []
        hr = DATA_HEADER_ROWS.get(tab, 1)
        return vals[hr:] if len(vals) > hr else []

    @staticmethod
    def _cell(row: list[Any], i: int) -> Any:
        return row[i] if i < len(row) else ""

    def read_all_stock_trades(self) -> list[dict[str, Any]]:
        """Read all stock rows with sheet row index, StockTrade object, status, and raw values."""
        from adapters.base import Broker, StockAction, StockTrade
        rows = self._read_data_rows(TAB_STOCKS, STOCKS_HEADERS)
        si = {h: i for i, h in enumerate(STOCKS_HEADERS)}
        hr = DATA_HEADER_ROWS.get(TAB_STOCKS, 1)
        out = []
        for offset, r in enumerate(rows):
            row_idx = hr + offset + 1
            act_str = str(self._cell(r, si["Action"])).strip()
            if not act_str:
                continue
            try:
                action = StockAction(act_str)
                trade_date = _parse_sheet_date(self._cell(r, si["Date"]))
                broker = Broker(str(self._cell(r, si["Broker"])).strip())
            except Exception:
                continue
            trade = StockTrade(
                date=trade_date,
                broker=broker,
                ticker=str(self._cell(r, si["Ticker"])).strip(),
                action=action,
                qty=self._cell(r, si["Qty"]),
                price=self._cell(r, si["Price"]),
                fee=self._cell(r, si["Fee"]) or 0,
                currency=str(self._cell(r, si["Currency"])).strip(),
                dedup_key=str(self._cell(r, si["_dedup_key"])).strip(),
            )
            status = str(self._cell(r, si["Status"])).strip() or "Open"
            out.append({
                "row_idx": row_idx,
                "trade": trade,
                "status": status,
                "raw": r,
            })
        return out

    def read_all_option_trades(self) -> list[dict[str, Any]]:
        """Read all option rows with sheet row index, OptionTrade object, status, and raw values."""
        from adapters.base import Broker, OptionAction, OptionTrade, OptionType
        rows = self._read_data_rows(TAB_OPTIONS, OPTIONS_HEADERS)
        oi = {h: i for i, h in enumerate(OPTIONS_HEADERS)}
        hr = DATA_HEADER_ROWS.get(TAB_OPTIONS, 1)
        out = []
        for offset, r in enumerate(rows):
            row_idx = hr + offset + 1
            act_str = str(self._cell(r, oi["Action"])).strip()
            if not act_str:
                continue
            try:
                action = OptionAction(act_str)
                trade_date = _parse_sheet_date(self._cell(r, oi["Date"]))
                expiry = _parse_sheet_date(self._cell(r, oi["Expiry"]))
                broker = Broker(str(self._cell(r, oi["Broker"])).strip())
                otype = OptionType(str(self._cell(r, oi["Type"])).strip())
            except Exception:
                continue
            trade = OptionTrade(
                date=trade_date,
                broker=broker,
                underlying=str(self._cell(r, oi["Stock"])).strip(),
                option_type=otype,
                strike=self._cell(r, oi["Strike"]),
                qty=self._cell(r, oi["Qty"]),
                expiry=expiry,
                action=action,
                premium=self._cell(r, oi["Premium"]),
                fee=self._cell(r, oi["Fee"]) or 0,
                currency=str(self._cell(r, oi["Currency"])).strip(),
                strategy=str(self._cell(r, oi["Strategy"])).strip(),
                dedup_key=str(self._cell(r, oi["_dedup_key"])).strip(),
            )
            status = str(self._cell(r, oi["Status"])).strip() or "Open"
            out.append({
                "row_idx": row_idx,
                "trade": trade,
                "status": status,
                "raw": r,
            })
        return out

    def update_status_batch(self, tab: str, updates: list[tuple[int, str]]) -> None:
        """Update the Status column (1-based row index) for a list of rows."""
        if not updates:
            return
        status_col = "J" if tab == TAB_STOCKS else ("N" if tab == TAB_OPTIONS else "")
        if not status_col:
            raise ValueError(f"Unknown tab for status update: {tab}")
        data = [
            {"range": f"{tab}!{status_col}{row_idx}", "values": [[status]]}
            for row_idx, status in updates
        ]
        self._client.batch_update_values(data)

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
                
                # Preserve manual Strategy overrides on Options tab
                if tab == TAB_OPTIONS and len(old_formatted) > 2 and old_formatted[2]:
                    new_formatted[2] = old_formatted[2]
                
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
        """Write header rows to tabs and apply styling."""
        tab_headers = {
            TAB_TRANSACTIONS: TRANSACTIONS_HEADERS,
            TAB_STOCKS:       STOCKS_HEADERS,
            TAB_OPTIONS:      OPTIONS_HEADERS,
            TAB_RUN_LOG:      RUN_LOG_HEADERS,
            TAB_DASHBOARD:    DASHBOARD_HEADERS,
        }
        write_data = []
        for tab, headers in tab_headers.items():
            header_row = DATA_HEADER_ROWS.get(tab, 1)
            # Force-write the header row every time: leftover data must never be
            # able to block it (a stale header row corrupts the whole layout).
            write_data.append({
                "range": f"{tab}!A{header_row}",
                "values": [[str(h) for h in headers]],
            })

            # Summary block for Stocks/Options: Total P/L (row 1) + Total Fees
            # (row 2), directly above the header row (row 3); data starts row 4.
            if tab in (TAB_STOCKS, TAB_OPTIONS):
                if tab == TAB_STOCKS:
                    # K = Realized P/L, H = Fee; data starts row 4.
                    write_data.append({
                        "range": f"{tab}!A1:B2",
                        "values": [
                            ["Total P/L", "=SUM(K4:K)"],
                            ["Total Fees", "=SUM(H4:H)"],
                        ]
                    })
                else:
                    # Options cols: A=Date B=Broker C=Strategy D=Stock E=Type
                    # F=Strike G=Qty H=Expiry I=Action J=Premium K=Total L=Fee
                    # M=Currency N=Status O=P/L P=P/L(SGD) Q=_dedup_key.
                    write_data.append({
                        "range": f"{tab}!A1:B2",
                        "values": [
                            ["Total P/L", "=SUM(O4:O)"],
                            ["Total Fees", "=SUM(L4:L)"],
                        ]
                    })

        if write_data:
            self._client.batch_update_values(write_data)
            
        # Always re-apply formatting (colors, number formats, filters, frozen rows)
        self._apply_formatting()

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

    def _apply_formatting(self) -> None:
        """Apply colors, frozen rows, filters, number formats — idempotently.

        Runs on every sync, so it must not accumulate state: it deletes existing
        conditional-format rules and resets the background first, then re-applies.
        Otherwise rules pile up and green creeps across the whole tab. Green is the
        header row only; Buy rows go green and Sell rows red via conditional rules.
        """
        tab_headers = {
            TAB_TRANSACTIONS: TRANSACTIONS_HEADERS,
            TAB_STOCKS:       STOCKS_HEADERS,
            TAB_OPTIONS:      OPTIONS_HEADERS,
        }

        # How many conditional-format rules each sheet already has, so we can
        # delete them before re-adding (prevents runaway accumulation).
        try:
            meta = self._client.get_sheet_metadata()
            cf_counts = {
                s["properties"]["sheetId"]: len(s.get("conditionalFormats", []))
                for s in meta.get("sheets", [])
            }
        except Exception:  # noqa: BLE001
            cf_counts = {}

        requests: list[dict] = []
        for tab, headers in tab_headers.items():
            sheet_id = self._sheet_ids.get(tab)
            if sheet_id is None:
                continue

            header_row_0 = DATA_HEADER_ROWS.get(tab, 1) - 1   # 0-based header row
            first_data_0 = header_row_0 + 1                    # 0-based first data row
            col_count = len(headers)

            # (a) Delete every existing conditional-format rule on this sheet.
            for _ in range(cf_counts.get(sheet_id, 0)):
                requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})

            # (b) CLEAR background, text colour and number format over the whole
            #     sheet — reverting to the theme default (so dark mode stays dark,
            #     light stays light) and wiping stale fills/formats. Crucially
            #     this also strips the green fill + white text that appended data
            #     rows inherit from the header. Specific formats re-applied below.
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0},
                    "cell": {},
                    "fields": ("userEnteredFormat.backgroundColor,"
                               "userEnteredFormat.textFormat.foregroundColor,"
                               "userEnteredFormat.numberFormat"),
                }
            })

            # (c) Header row only: green background, white bold text.
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": header_row_0,
                              "endRowIndex": header_row_0 + 1, "startColumnIndex": 0,
                              "endColumnIndex": col_count},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 0.15, "green": 0.5, "blue": 0.25},
                        "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })

            # (d) Summary block (rows 1-2, cols A-B): light grey, bold.
            if tab in (TAB_STOCKS, TAB_OPTIONS):
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 2,
                                  "startColumnIndex": 0, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95},
                            "textFormat": {"bold": True},
                        }},
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                })

            # (e) Freeze the header zone (summary + header rows).
            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": header_row_0 + 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            })

            # (f) The basic filter (which hides Closed rows) is owned by
            #     sort_data_tabs, not here: it must be cleared *around* the sort so
            #     the sort reorders ALL rows, then re-applied. status_col is still
            #     needed for the Closed->grey conditional format below.
            status_col = 9 if tab == TAB_STOCKS else (13 if tab == TAB_OPTIONS else None)

            # (g) Conditional: Buy -> green, Sell -> red (Action column, data rows).
            action_col = 3 if tab == TAB_STOCKS else (8 if tab == TAB_OPTIONS else None)
            if action_col is not None:
                for action, rgb in [("Buy", {"red": 0.84, "green": 0.94, "blue": 0.82}),
                                     ("Sell", {"red": 0.96, "green": 0.82, "blue": 0.82})]:
                    requests.append({"addConditionalFormatRule": {"rule": {
                        "ranges": [{"sheetId": sheet_id, "startRowIndex": first_data_0,
                                    "startColumnIndex": action_col, "endColumnIndex": action_col + 1}],
                        "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": action}]},
                                        "format": {"backgroundColor": rgb}},
                    }, "index": 0}})

            # (h) Conditional: Closed status -> grey.
            if status_col is not None:
                requests.append({"addConditionalFormatRule": {"rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": first_data_0,
                                "startColumnIndex": status_col, "endColumnIndex": status_col + 1}],
                    "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Closed"}]},
                                    "format": {"backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
                                               "textFormat": {"foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}}},
                }, "index": 0}})

            # (i) Conditional: option expiring THIS week (today through the coming
            #     Saturday) -> yellow. `TODAY()+MOD(6-WEEKDAY(TODAY(),2),7)` is the
            #     upcoming Saturday (WEEKDAY(...,2): Mon=1..Sun=7; Sat=6), so a
            #     next-week expiry like Aug 19 isn't highlighted, only the current
            #     week's Fri/Sat expiries.
            if tab == TAB_OPTIONS:
                expiry_col = 7  # Column H
                cell_ref = f"H{first_data_0 + 1}"  # 1-based first data row
                requests.append({"addConditionalFormatRule": {"rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": first_data_0,
                                "startColumnIndex": expiry_col, "endColumnIndex": expiry_col + 1}],
                    "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue":
                                    f'=AND({cell_ref}<>"", {cell_ref}>=TODAY(), '
                                    f'{cell_ref}<=TODAY()+MOD(6-WEEKDAY(TODAY(),2),7))'}]},
                                    "format": {"backgroundColor": {"red": 1, "green": 1, "blue": 0.8}}},
                }, "index": 0}})

            # (j) TEXT format for ticker/underlying so zero-padded symbols (HK
            #     "07709") aren't coerced to numbers (would break reconciliation).
            text_col = 2 if tab == TAB_STOCKS else (3 if tab == TAB_OPTIONS else None)
            if text_col is not None:
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": first_data_0,
                              "startColumnIndex": text_col, "endColumnIndex": text_col + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})

            # (k) Currency format for money columns.
            money_cols = []
            if tab == TAB_STOCKS:
                money_cols = [5, 6, 7, 10, 11]         # Price, Total, Fee, Realized P/L, SGD
            elif tab == TAB_OPTIONS:
                money_cols = [5, 9, 10, 11, 14, 15]    # Strike, Premium, Total, Fee, P/L, SGD
            elif tab == TAB_TRANSACTIONS:
                money_cols = [3, 5]                    # Amount, Amount (SGD)
            for col in money_cols:
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": first_data_0,
                              "startColumnIndex": col, "endColumnIndex": col + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "\"$\"#,##0.00"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})

            # (l) Date columns -> ISO date display (stored as Sheets serials).
            date_cols = [0, 7] if tab == TAB_OPTIONS else [0]  # Options also has Expiry (H)
            for col in date_cols:
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": first_data_0,
                              "startColumnIndex": col, "endColumnIndex": col + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})

            # (m) Summary totals (B1:B2) -> currency.
            if tab in (TAB_STOCKS, TAB_OPTIONS):
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 2,
                              "startColumnIndex": 1, "endColumnIndex": 2},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY", "pattern": "\"$\"#,##0.00"}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})

        if requests:
            self._client.batch_update(requests)


# --------------------------------------------------------------------------- #
# Column-letter helper
# --------------------------------------------------------------------------- #

_SHEETS_EPOCH = date(1899, 12, 30)  # Sheets/Excel day 0


def _parse_sheet_date(value: object) -> date:
    """Parse a date read back from Sheets.

    UNFORMATTED reads return dates as serial day-counts from 1899-12-30; older
    hand-entered cells may be ISO strings. Handles both.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise ValueError(f"cannot parse date from bool {value!r}")
    if isinstance(value, (int, float)):
        from datetime import timedelta
        return _SHEETS_EPOCH + timedelta(days=int(value))
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"cannot parse sheet date value {value!r}")


def sheet_date_to_iso(value: object) -> str:
    """Normalize a Date/Expiry cell read from the sheet to ISO ``yyyy-mm-dd``.

    ``get_values`` reads ``userEnteredValue``, so a date cell arrives as its
    serial ``numberValue`` (e.g. ``46251``), not a formatted string — parse that
    to a real date. Blank -> ``''``. An unparseable value passes through as its
    stripped string, so downstream 'no valid date' handling still applies instead
    of crashing the whole read.
    """
    if value is None or value == "":
        return ""
    try:
        return _parse_sheet_date(value).isoformat()
    except (ValueError, TypeError):
        return str(value).strip()


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

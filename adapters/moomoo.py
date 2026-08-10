"""MooMoo adapter (step 8 of the build order — BUILD_SPEC.md §2, §3).

MooMoo differs from Tiger/Longbridge: the SDK (`moomoo`, pip `moomoo-api`) does
not talk to MooMoo directly — it connects to a local **OpenD gateway**, a
persistent process that holds the session. In production OpenD runs as a sidecar
container next to the job (Cloud Run multi-container, §2); see `opend/`. The
adapter just needs the gateway's host/port and the account's security firm
(`FUTUSG` for the Singapore account).

Conforms to :class:`~adapters.base.BrokerAdapter` and returns only the common
schema. Field mapping was derived against the installed SDK surface:

* Executions ← ``history_order_list_query`` filtered to filled orders (one row
  per order, matching the Tiger/Longbridge shape and giving us ``currency`` and
  ``dealt_avg_price``). Fees ← ``order_fee_query`` (fee is per *order*), joined
  on ``order_id``.
* Positions ← ``position_list_query`` (STK + OPT in one call; split by option
  code). Short positions are returned with ``position_side == SHORT`` and are
  **signed negative** so they line up with the FIFO engine's signed holdings and
  with :func:`core.reconcile.reconcile`.
* Cash movements: MooMoo *does* expose ``get_acc_cash_flow``, but it has no
  Deposit/Withdrawal classification — verified against a live SG account, real
  funding surfaces only as ``Auto Currency Exchange`` ("TRANSFER FROM UNIVERSAL
  SECURITIES ACCOUNT") and an ambiguous ``Others`` bucket dominated by
  trade-settlement IN/OUT pairs. Auto-classifying that would corrupt Net Capital
  In, so this returns ``[]`` and MooMoo cash flows are hand-entered (§14 fallback).

MooMoo queries are read-only, so no ``unlock_trade`` (trading password) is
needed. Every query returns ``(ret_code, data)``; a non-OK code raises
(fail loud, §9).

Assumptions surfaced (§14): option ``multiplier`` is assumed 100 (positions/
orders don't report it); option ``code`` is OCC-style
(``US.AAPL240119C00190000``) and parsed accordingly; premium is treated as
per-share. Realized P/L is computed downstream by the FIFO engine, not here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Optional

from moomoo import (
    OpenSecTradeContext,
    OrderStatus,
    PositionSide,
    SecurityFirm,
    SysConfig,
    TrdEnv,
    TrdMarket,
    TrdSide,
    RET_OK,
)

from adapters.base import (
    AssetType,
    Broker,
    CashMovement,
    OptionAction,
    OptionTrade,
    OptionType,
    Position,
    StockAction,
    StockTrade,
    dec,
)

# Filled (fully or partially) — the only orders that represent executions.
_FILLED_STATUSES = [OrderStatus.FILLED_ALL, OrderStatus.FILLED_PART]

# Market prefix -> native currency (used when a row has no currency column).
_MARKET_CCY = {"US": "USD", "HK": "HKD", "SG": "SGD", "CN": "CNH"}

# Option code body: <UNDERLYING><YYMMDD><C|P><strike*1000>. MooMoo does NOT
# zero-pad the strike to 8 digits (e.g. "SHOP260821C145000" = strike 145.00),
# so the strike group is variable-length. OCC-style 8-digit codes still match.
_OPTION_RE = re.compile(r"^(?P<u>[A-Z]+)(?P<d>\d{6})(?P<cp>[CP])(?P<s>\d+)$")

_OPTION_MULTIPLIER = Decimal("100")  # §14 assumption


def _missing(value) -> bool:
    """True for values that mean 'no data': None, NaN, blank, or 'N/A'.

    DataFrame -> ``to_dict`` turns absent cells into float ``NaN``, so a plain
    ``in (None, "")`` check is not enough.
    """
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and value.strip() in ("", "N/A", "nan", "NaN"):
        return True
    return False


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass
class MooMooCredentials:
    """OpenD gateway connection details (not broker secrets — those live in
    OpenD itself). Read from the secret store / env; nothing hard-coded (§9)."""

    host: str = "127.0.0.1"
    port: int = 11111
    security_firm: str = "FUTUSG"  # MooMoo Singapore
    trd_env: str = "REAL"  # REAL | SIMULATE
    acc_id: int = 0  # 0 = first account for the firm
    markets: tuple[str, ...] = ("US", "HK")
    timezone: str = "Asia/Singapore"

    @classmethod
    def from_env(cls, prefix: str = "MOOMOO_") -> "MooMooCredentials":
        markets = os.environ.get(prefix + "MARKETS", "US,HK")
        return cls(
            host=os.environ.get(prefix + "HOST", "127.0.0.1"),
            port=int(os.environ.get(prefix + "PORT", "11111")),
            security_firm=os.environ.get(prefix + "SECURITY_FIRM", "FUTUSG"),
            trd_env=os.environ.get(prefix + "TRD_ENV", "REAL"),
            acc_id=int(os.environ.get(prefix + "ACC_ID", "0")),
            markets=tuple(m.strip().upper() for m in markets.split(",") if m.strip()),
            timezone=os.environ.get(prefix + "TIMEZONE", "Asia/Singapore"),
        )


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class MooMooAdapter:
    name: str = Broker.MOOMOO.value

    def __init__(
        self,
        credentials: Optional[MooMooCredentials] = None,
        *,
        context_factory: Optional[Callable[[str], object]] = None,
        markets: Optional[tuple[str, ...]] = None,
        cash_movements_enabled: bool = True,
    ) -> None:
        """Provide ``credentials`` (normal path) or a ``context_factory`` that
        returns a trade context per market (for tests). ``markets`` overrides the
        markets to query."""
        if credentials is None and context_factory is None:
            raise ValueError("provide either credentials or a context_factory")

        self._creds = credentials
        self._context_factory = context_factory or self._build_context
        self._markets = markets or (credentials.markets if credentials else ("US",))
        self._trd_env = credentials.trd_env if credentials else "REAL"
        self._acc_id = credentials.acc_id if credentials else 0
        self._cash_enabled = cash_movements_enabled
        self._ctx_cache: dict[str, object] = {}

    # -- context management ------------------------------------------------- #
    def _build_context(self, market: str):
        assert self._creds is not None
        # OpenD SDK spins up background connection threads; make them daemon so a
        # wedged gateway can never block the process from exiting.
        SysConfig.set_all_thread_daemon(True)
        return OpenSecTradeContext(
            filter_trdmarket=getattr(TrdMarket, market),
            host=self._creds.host,
            port=self._creds.port,
            security_firm=getattr(SecurityFirm, self._creds.security_firm),
        )

    def _context(self, market: str):
        if market not in self._ctx_cache:
            self._ctx_cache[market] = self._context_factory(market)
        return self._ctx_cache[market]

    def close(self) -> None:
        """Close all cached OpenD contexts. Leaked contexts hold gateway
        connections that can wedge OpenD across runs, so the orchestrator calls
        this after each fetch. Safe to call more than once."""
        for ctx in self._ctx_cache.values():
            close = getattr(ctx, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    pass
        self._ctx_cache.clear()

    @staticmethod
    def _unwrap(result, what: str):
        """Split MooMoo's ``(ret_code, data)`` return; raise on non-OK (§9)."""
        ret, data = result
        if ret != RET_OK:
            raise RuntimeError(f"MooMoo {what} failed: {data}")
        return data

    # -- executions --------------------------------------------------------- #
    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]:
        trades: list[StockTrade] = []
        seen: set[str] = set()  # OpenD ignores the market filter and returns all
        for market in self._markets:                     # markets per query, so
            rows, fees = self._filled_orders_with_fees(market, since)  # dedup by
            for row in rows:                                          # order_id.
                oid = str(row["order_id"])
                if oid in seen:
                    continue
                seen.add(oid)
                ticker, opt = self._parse_code(row["code"])
                if opt is not None:
                    continue  # options handled separately
                qty = dec(row["dealt_qty"])
                if qty == 0:
                    continue
                trades.append(
                    StockTrade(
                        date=self._row_date(row),
                        broker=Broker.MOOMOO,
                        ticker=ticker,
                        action=self._stock_action(row["trd_side"]),
                        qty=qty,
                        price=dec(row["dealt_avg_price"]),
                        fee=fees.get(str(row["order_id"]), Decimal("0")),
                        currency=self._row_currency(row, ticker_market=market),
                        fill_id=str(row["order_id"]),
                    )
                )
        return trades

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]:
        trades: list[OptionTrade] = []
        seen: set[str] = set()  # dedup across markets (see fetch_stock_executions)
        for market in self._markets:
            rows, fees = self._filled_orders_with_fees(market, since)
            for row in rows:
                oid = str(row["order_id"])
                if oid in seen:
                    continue
                seen.add(oid)
                underlying, opt = self._parse_code(row["code"])
                if opt is None:
                    continue  # stocks handled separately
                qty = dec(row["dealt_qty"])
                if qty == 0:
                    continue
                otype, strike, expiry = opt
                trades.append(
                    OptionTrade(
                        date=self._row_date(row),
                        broker=Broker.MOOMOO,
                        underlying=underlying,
                        option_type=otype,
                        strike=strike,
                        qty=qty,
                        expiry=expiry,
                        action=self._option_action(row["trd_side"]),
                        premium=dec(row["dealt_avg_price"]),
                        fee=fees.get(str(row["order_id"]), Decimal("0")),
                        currency=self._row_currency(row, ticker_market=market),
                        multiplier=_OPTION_MULTIPLIER,
                        fill_id=str(row["order_id"]),
                    )
                )
        return trades

    def _filled_orders_with_fees(self, market: str, since: date | None):
        """Return (list-of-order-dicts, {order_id: fee}) for filled orders."""
        ctx = self._context(market)
        start = since.isoformat() if since else ""
        df = self._unwrap(
            ctx.history_order_list_query(
                status_filter_list=_FILLED_STATUSES,
                start=start,
                end="",
                trd_env=self._trd_env,
                acc_id=self._acc_id,
            ),
            "history_order_list_query",
        )
        rows = df.to_dict("records") if df is not None and not df.empty else []
        order_ids = [str(r["order_id"]) for r in rows]
        return rows, self._fees_for(ctx, order_ids)

    def _fees_for(self, ctx, order_ids: list[str]) -> dict[str, Decimal]:
        if not order_ids:
            return {}
        df = self._unwrap(
            ctx.order_fee_query(
                order_id_list=order_ids, trd_env=self._trd_env, acc_id=self._acc_id
            ),
            "order_fee_query",
        )
        fees: dict[str, Decimal] = {}
        if df is not None and not df.empty:
            for r in df.to_dict("records"):
                raw = r.get("fee_amount")
                fees[str(r["order_id"])] = Decimal("0") if _missing(raw) else abs(dec(raw))
        return fees

    # -- positions (seeding + reconciliation, §5/§9) ------------------------ #
    def fetch_positions(self) -> list[Position]:
        positions: list[Position] = []
        seen: set[str] = set()  # OpenD returns all positions per market query;
        today = datetime.now().date()          # dedup by code to avoid doubling.
        for market in self._markets:
            ctx = self._context(market)
            df = self._unwrap(
                ctx.position_list_query(trd_env=self._trd_env, acc_id=self._acc_id),
                "position_list_query",
            )
            if df is None or df.empty:
                continue
            for row in df.to_dict("records"):
                code = str(row["code"])
                if code in seen:
                    continue
                seen.add(code)
                qty = dec(row["qty"])
                if qty == 0:
                    continue
                # Sign short positions negative to match FIFO holdings / reconcile.
                if str(row.get("position_side", "")).upper() == PositionSide.SHORT:
                    qty = -abs(qty)
                symbol, opt = self._parse_code(row["code"])
                market_price = (
                    None if _missing(row.get("nominal_price")) else dec(row["nominal_price"])
                )
                if opt is None:
                    positions.append(
                        Position(
                            broker=Broker.MOOMOO,
                            asset_type=AssetType.STOCK,
                            symbol=symbol,
                            qty=qty,
                            avg_cost=dec(row["cost_price"]),
                            currency=self._row_currency(row, ticker_market=market),
                            market_price=market_price,
                            as_of=today,
                        )
                    )
                else:
                    otype, strike, expiry = opt
                    positions.append(
                        Position(
                            broker=Broker.MOOMOO,
                            asset_type=AssetType.OPTION,
                            symbol=symbol,
                            qty=qty,
                            avg_cost=dec(row["cost_price"]),
                            currency=self._row_currency(row, ticker_market=market),
                            market_price=market_price,
                            as_of=today,
                            option_type=otype,
                            strike=strike,
                            expiry=expiry,
                            multiplier=_OPTION_MULTIPLIER,
                        )
                    )
        return positions

    # -- cash movements ----------------------------------------------------- #
    def fetch_cash_movements(self, since: date | None) -> list[CashMovement]:
        """Always returns an empty list — MooMoo cash flows are hand-entered (§14).

        ``get_acc_cash_flow`` exists but exposes no reliable Deposit/Withdrawal
        type (see module docstring), so there is nothing safe to auto-classify.
        """
        return []

    # -- field helpers ------------------------------------------------------ #
    @staticmethod
    def _parse_code(code: str):
        """('US.AAPL' -> 'AAPL', None) for stocks;
        ('US.AAPL240119C00190000' -> 'AAPL', (OptionType, strike, expiry)) for options."""
        body = str(code).split(".")[-1].strip()
        m = _OPTION_RE.match(body)
        if not m:
            return body, None
        otype = OptionType.CALL if m.group("cp") == "C" else OptionType.PUT
        strike = dec(int(m.group("s"))) / Decimal("1000")
        d = m.group("d")
        expiry = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
        return m.group("u"), (otype, strike, expiry)

    @staticmethod
    def _row_currency(row: dict, *, ticker_market: str) -> str:
        ccy = row.get("currency")
        if not _missing(ccy):
            return str(ccy)
        # Derive from the code's market prefix, else the queried market.
        code = str(row.get("code", ""))
        prefix = code.split(".")[0] if "." in code else ticker_market
        return _MARKET_CCY.get(prefix.upper(), _MARKET_CCY.get(ticker_market.upper(), ""))

    @staticmethod
    def _row_date(row: dict) -> date:
        raw = row.get("updated_time") or row.get("create_time") or ""
        s = str(raw).strip()
        if not s:
            raise ValueError(f"MooMoo order {row.get('order_id')} has no timestamp")
        return datetime.strptime(s[:10], "%Y-%m-%d").date()

    @staticmethod
    def _stock_action(trd_side) -> StockAction:
        return StockAction.BUY if MooMooAdapter._is_buy(trd_side) else StockAction.SELL

    @staticmethod
    def _option_action(trd_side) -> OptionAction:
        return OptionAction.BUY if MooMooAdapter._is_buy(trd_side) else OptionAction.SELL

    @staticmethod
    def _is_buy(trd_side) -> bool:
        # BUY and BUY_BACK (buy-to-close) are acquisitions; SELL and SELL_SHORT
        # (sell-to-open) reduce/short. The signed FIFO engine sorts out open/close.
        return str(trd_side).upper() in (str(TrdSide.BUY), str(TrdSide.BUY_BACK))

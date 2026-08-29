"""MooMoo adapter (step 8 of the build order — BUILD_SPEC.md §2, §3).

MooMoo differs from Tiger/Longbridge: the SDK (`moomoo`, pip `moomoo-api`) does
not talk to MooMoo directly — it connects to a local **OpenD gateway**, a
persistent process that holds the session. In production OpenD runs as a sidecar
container next to the job (Cloud Run multi-container, §2); see `opend/`. The
adapter just needs the gateway's host/port and the account's security firm
(`FUTUSG` for the Singapore account).

Conforms to :class:`~adapters.base.BrokerAdapter` and returns only the common
schema. Field mapping was derived against the installed SDK surface:

* Executions ← discovered by FILL time via ``history_deal_list_query`` (a deal
  is created at fill time, so a resting order placed before the window but
  filled inside it is captured — ``history_order_list_query`` filters by order
  time and would miss it). Order detail (``currency``, ``dealt_avg_price``,
  combo code) is joined in from ``history_order_list_query`` and dated by the
  deal's fill time. Fees ← ``order_fee_query`` (fee is per *order*), joined on
  ``order_id``.
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
from datetime import date, datetime, timedelta
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
    is_option_code,
    parse_option_legs,
)

# Filled (fully or partially) — the only orders that represent executions.
_FILLED_STATUSES = [OrderStatus.FILLED_ALL, OrderStatus.FILLED_PART]

# Market prefix -> native currency (used when a row has no currency column).
_MARKET_CCY = {"US": "USD", "HK": "HKD", "SG": "SGD", "CN": "CNH"}

_OPTION_MULTIPLIER = Decimal("100")  # §14 assumption


def _combo_leg_buy_flags(legs, is_buy: bool) -> list[bool]:
    """Per-leg BUY(True)/SELL(False) for a decomposed combo order.

    MooMoo returns one row for the whole combo — the strikes, but not per-leg
    direction — so an assumption is unavoidable.

    For any combo where every option-type group has exactly 2 legs, we apply
    the standard debit-spread convention *per group*:
      * Calls: *buying* = long the lower strike, short the higher.
      * Puts:  *buying* = long the higher strike, short the lower.
    This covers 2-leg verticals AND 4-leg iron condors (two put legs + two
    call legs). *Selling* flips all legs.

    Anything that doesn't decompose neatly into 2-leg-per-type groups keeps
    the old fallback: leg 0 takes the order side, the rest oppose it.
    """
    # Group leg indices by option type
    by_type: dict[OptionType, list[int]] = {}
    for i, leg in enumerate(legs):
        by_type.setdefault(leg[1], []).append(i)

    # If every type has exactly 2 legs, apply vertical-spread convention per type
    if all(len(idxs) == 2 for idxs in by_type.values()):
        flags = [False] * len(legs)
        for otype, idxs in by_type.items():
            strikes = [legs[i][2] for i in idxs]
            long_strike = min(strikes) if otype == OptionType.CALL else max(strikes)
            long_idx = idxs[strikes.index(long_strike)]
            short_idx = idxs[1 - strikes.index(long_strike)]
            flags[long_idx] = is_buy
            flags[short_idx] = not is_buy
        return flags

    return [is_buy if i == 0 else not is_buy for i in range(len(legs))]



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
                if is_option_code(str(row["code"])):
                    continue  # options (single or combo) handled separately
                qty = dec(row["dealt_qty"])
                if qty == 0:
                    continue
                ticker = str(row["code"]).split(".")[-1].strip()
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
                legs = parse_option_legs(str(row["code"]))
                if legs is None:
                    continue  # stocks handled separately
                qty = dec(row["dealt_qty"])
                if qty == 0:
                    continue
                order_date = self._row_date(row)
                order_fee = fees.get(str(row["order_id"]), Decimal("0"))
                ccy = self._row_currency(row, ticker_market=market)
                is_buy = self._is_buy(row["trd_side"])

                if len(legs) == 1:
                    underlying, otype, strike, expiry = legs[0]
                    trades.append(
                        OptionTrade(
                            date=order_date,
                            broker=Broker.MOOMOO,
                            underlying=underlying,
                            option_type=otype,
                            strike=strike,
                            qty=qty,
                            expiry=expiry,
                            action=self._option_action(row["trd_side"]),
                            premium=dec(row["dealt_avg_price"]),
                            fee=order_fee,
                            currency=ccy,
                            multiplier=_OPTION_MULTIPLIER,
                            fill_id=str(row["order_id"]),
                        )
                    )
                else:
                    # Multi-leg combo spread (e.g. SHOP260821P130/145). MooMoo gives
                    # one row for the whole combo, so per-leg direction is inferred
                    # (see _combo_leg_buy_flags). Whole-order fee & premium land on
                    # the leg that matches the order's own side, so the combo's net
                    # debit/credit keeps the right sign.
                    buy_flags = _combo_leg_buy_flags(legs, is_buy)
                    primary_idx = next(i for i, f in enumerate(buy_flags) if f == is_buy)
                    for i, (underlying, otype, strike, expiry) in enumerate(legs):
                        leg_action = OptionAction.BUY if buy_flags[i] else OptionAction.SELL
                        trades.append(
                            OptionTrade(
                                date=order_date,
                                broker=Broker.MOOMOO,
                                underlying=underlying,
                                option_type=otype,
                                strike=strike,
                                qty=qty,
                                expiry=expiry,
                                action=leg_action,
                                premium=dec(row["dealt_avg_price"]) if i == primary_idx else Decimal("0"),
                                fee=order_fee if i == primary_idx else Decimal("0"),
                                currency=ccy,
                                multiplier=_OPTION_MULTIPLIER,
                                fill_id=f"{oid}:{i}",
                            )
                        )
        return trades

    def _filled_orders_with_fees(self, market: str, since: date | None):
        """Return (list-of-order-dicts, {order_id: fee}) for orders FILLED in the
        window.

        Inclusion and dating come from history_deal_list_query — a *deal* is
        created at fill time — so a resting order placed before ``since`` but
        filled inside the window is captured. history_order_list_query filters by
        order time and would silently miss it. Order *detail* (currency,
        dealt_avg_price, combo code) is joined in from history_order_list_query,
        with the deal's fill date overriding the order's row date.

        ``since`` is intentionally widened to the API's 90-day maximum (both
        endpoints hard-cap the lookback there); run.py drops the pre-cutoff
        overshoot by our own fill date. An order filled in-window but *placed*
        more than 90 days ago is beyond both endpoints' reach and is skipped with
        a warning rather than silently dropped."""
        ctx = self._context(market)
        start = (datetime.now().date() - timedelta(days=89)).isoformat()

        deals = self._unwrap(
            ctx.history_deal_list_query(
                start=start, end="", trd_env=self._trd_env, acc_id=self._acc_id
            ),
            "history_deal_list_query",
        )
        fill_date: dict[str, str] = {}  # order_id -> latest fill date (YYYY-MM-DD)
        if deals is not None and not deals.empty:
            for d in deals.to_dict("records"):
                oid = str(d["order_id"])
                ct = str(d.get("create_time", ""))[:10]
                if ct and (oid not in fill_date or ct > fill_date[oid]):
                    fill_date[oid] = ct
        if not fill_date:
            return [], {}

        odf = self._unwrap(
            ctx.history_order_list_query(
                status_filter_list=_FILLED_STATUSES, start=start, end="",
                trd_env=self._trd_env, acc_id=self._acc_id,
            ),
            "history_order_list_query",
        )
        order_rows: dict[str, dict] = {}
        if odf is not None and not odf.empty:
            for r in odf.to_dict("records"):
                order_rows[str(r["order_id"])] = r

        rows: list[dict] = []
        for oid, fdate in fill_date.items():
            r = order_rows.get(oid)
            if r is None:
                print(f"Warning: MooMoo order {oid} filled in window but its "
                      f"detail is unavailable (placed >90d ago); skipped")
                continue
            r = dict(r)
            r["updated_time"] = fdate  # date by the deal's fill time
            rows.append(r)
        return rows, self._fees_for(ctx, list(fill_date.keys()))

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
                legs = parse_option_legs(str(row["code"]))
                market_price = (
                    None if _missing(row.get("nominal_price")) else dec(row["nominal_price"])
                )
                if not legs:
                    ticker = str(row["code"]).split(".")[-1].strip()
                    positions.append(
                        Position(
                            broker=Broker.MOOMOO,
                            asset_type=AssetType.STOCK,
                            symbol=ticker,
                            qty=qty,
                            avg_cost=dec(row["cost_price"]),
                            currency=self._row_currency(row, ticker_market=market),
                            name=str(row.get("stock_name", "") or ""),
                            market_price=market_price,
                            as_of=today,
                        )
                    )
                else:
                    for underlying, otype, strike, expiry in legs:
                        positions.append(
                            Position(
                                broker=Broker.MOOMOO,
                                asset_type=AssetType.OPTION,
                                symbol=underlying,
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

    # -- account value (§4 dashboard) --------------------------------------- #
    def fetch_account_value(self) -> list[tuple[Decimal, str]]:
        """Total account assets as (amount, currency). ``accinfo_query`` is
        account-wide (OpenD ignores the market filter), so query once."""
        ctx = self._context(self._markets[0])
        ret, data = ctx.accinfo_query(refresh_cache=True)
        if ret != 0 or not hasattr(data, "iterrows"):
            return []
        out: list[tuple[Decimal, str]] = []
        for _, row in data.iterrows():
            total = row.get("total_assets")
            if total is not None:
                out.append((Decimal(str(total)), str(row.get("currency", "HKD") or "HKD")))
        return out

    # -- field helpers ------------------------------------------------------ #
    @staticmethod
    def _parse_code(code: str):
        """('US.AAPL' -> 'AAPL', None) for stocks;
        ('US.AAPL240119C00190000' -> 'AAPL', (OptionType, strike, expiry)) for options."""
        legs = parse_option_legs(str(code))
        if not legs:
            body = str(code).split(".")[-1].strip()
            return body, None
        underlying, otype, strike, expiry = legs[0]
        return underlying, (otype, strike, expiry)

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

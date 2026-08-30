"""Longbridge adapter (step 6 of the build order — BUILD_SPEC.md §3).

Conforms to the :class:~adapters.base.BrokerAdapter protocol.

Auth: App Key + App Secret + Access Token (OpenAPI).
Field mapping:
- Executions: discovered by FILL time via history_executions (so a resting
  order placed before the window but filled inside it is captured, not dropped
  as history_orders — which filters by order time — would). Each fill's
  order_id is joined to order_detail(order_id) for the full order (side,
  symbol, currency, executed qty/price) AND the fee
  (charge_detail.total_amount) in a single call. Trades are dated by the fill's
  trade_done_at, not the order's updated_at.
- Positions: stock_positions for STK, no options API available.
- Cash: cash_flow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from longport.openapi import (
    Config,
    TradeContext,
    OrderSide,
    CashFlowDirection,
)

from adapters.base import (
    AssetType,
    Broker,
    CashMovement,
    CashType,
    OptionAction,
    OptionTrade,
    OptionType,
    Position,
    StockAction,
    StockTrade,
    dec,
    is_option_code,
    parse_option_code,
    parse_option_legs,
)


def _combo_leg_buy_flags(legs, is_buy: bool) -> list[bool]:
    """Determine per-leg buy/sell directions for a combo order."""
    if len(legs) == 2 and legs[0][1] == legs[1][1]:
        otype = legs[0][1]
        strikes = [leg[2] for leg in legs]
        long_strike = min(strikes) if otype == OptionType.CALL else max(strikes)
        long_idx = strikes.index(long_strike)
        return [is_buy if i == long_idx else not is_buy for i in range(2)]
    return [is_buy if i == 0 else not is_buy for i in range(len(legs))]


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass
class LongbridgeCredentials:
    app_key: str
    app_secret: str
    access_token: str
    timezone: str = "Asia/Singapore"

    @classmethod
    def from_env(cls, prefix: str = "LONGBRIDGE_") -> "LongbridgeCredentials":
        def _req(name: str) -> str:
            val = os.environ.get(prefix + name)
            if not val:
                raise ValueError(f"missing required env var {prefix + name}")
            return val

        return cls(
            app_key=_req("APP_KEY"),
            app_secret=_req("APP_SECRET"),
            access_token=_req("ACCESS_TOKEN"),
            timezone=os.environ.get(prefix + "TIMEZONE", "Asia/Singapore"),
        )


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class LongbridgeAdapter:
    name: str = Broker.LONGBRIDGE.value

    def __init__(
        self,
        credentials: Optional[LongbridgeCredentials] = None,
        *,
        client: Optional[TradeContext] = None,
        timezone: Optional[str] = None,
        cash_movements_enabled: bool = True,
    ) -> None:
        if client is None and credentials is None:
            raise ValueError("provide either credentials or a client")

        tz_name = timezone or (credentials.timezone if credentials else "Asia/Singapore")
        self._tz = ZoneInfo(tz_name)
        self._cash_enabled = cash_movements_enabled

        if client is not None:
            self._client = client
        else:
            assert credentials is not None
            config = Config.from_apikey(
                app_key=credentials.app_key,
                app_secret=credentials.app_secret,
                access_token=credentials.access_token,
            )
            self._client = TradeContext(config)

    # -- time helpers ------------------------------------------------------- #
    def _since_to_datetime(self, since: date | None) -> Optional[datetime]:
        if since is None:
            return None
        return datetime(since.year, since.month, since.day, tzinfo=self._tz)
        
    def _timestamp_to_date(self, ts: float) -> date:
        return datetime.fromtimestamp(ts, tz=self._tz).date()

    # -- retry helper ------------------------------------------------------- #
    def _call_with_retry(self, fn, *args, **kwargs):
        """Execute a Longbridge API call with retry on rate limit (code 429002)."""
        import time
        for attempt in range(4):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                err_str = str(e).lower()
                if ("429" in err_str or "limited" in err_str or "frequency" in err_str) and attempt < 3:
                    time.sleep((attempt + 1) * 2.0)
                else:
                    raise

    # -- executions --------------------------------------------------------- #
    def _order_detail(self, order_id: str):
        """Fetch an order's full detail (fields + charge_detail), with a
        rate-limit retry. Returns None if it can't be read."""
        import time
        import re
        for attempt in range(3):
            try:
                time.sleep(0.1)  # small throttle to avoid bursting API limit
                return self._client.order_detail(order_id)
            except Exception as e:  # noqa: BLE001
                err_str = str(e).lower()
                if ("429" in err_str or "limited" in err_str) and attempt < 2:
                    # e.g. "rate limit of 30-second interval has been reached, please retry after: 19.5s"
                    m = re.search(r"retry after:\s*([0-9.]+)", err_str)
                    sleep_time = float(m.group(1)) + 0.5 if m else 2.0 * (attempt + 1)
                    time.sleep(sleep_time)
                else:
                    print(f"Warning: Could not fetch order_detail for {order_id}: {e}")
                    return None
        return None

    @staticmethod
    def _fee(detail) -> Decimal:
        cd = getattr(detail, "charge_detail", None)
        if cd is not None:
            return dec(getattr(cd, "total_amount", None) or "0")
        return Decimal("0")

    @staticmethod
    def _has_option_fees(detail) -> bool:
        """Check if charge_detail contains options-specific fees."""
        cd = getattr(detail, "charge_detail", None)
        if cd is None:
            return False
        items = getattr(cd, "items", []) or []
        for item in items:
            fees = getattr(item, "fees", []) or []
            for f in fees:
                code = str(getattr(f, "code", "") or "")
                if code.startswith("Options") or "option" in code.lower():
                    return True
        return False

    @staticmethod
    def _has_fee_data(detail) -> bool:
        """True if the order carries any fee information (non-zero total or a fee
        line item).

        Longbridge's misreported option-combo closes come back with a completely
        empty ``charge_detail`` (``total_amount`` 0, no fee items); genuine stock
        and option fills normally carry a fee. Used to narrow the price
        sanity-check in ``fetch_stock_executions`` to only the ambiguous zero-fee
        fills, so normal stock trades are never second-guessed."""
        cd = getattr(detail, "charge_detail", None)
        if cd is None:
            return False
        total = getattr(cd, "total_amount", None)
        try:
            if total is not None and Decimal(str(total)) != 0:
                return True
        except (ArithmeticError, ValueError, TypeError):
            pass
        for item in getattr(cd, "items", []) or []:
            if getattr(item, "fees", None):
                return True
        return False

    # A "stock" fill priced below this fraction of the live share price is really
    # an option premium misreported under the bare underlying symbol. 0.4 sits far
    # from both a real fill (~1.0 of spot) and a premium (typically <0.1 of spot).
    _STOCK_PRICE_FLOOR_FRAC = Decimal("0.4")

    @staticmethod
    def _live_price(code: str) -> Optional[Decimal]:
        """Best-effort last share price via yfinance; None on any failure.

        Kept self-contained (no analytics-layer import) so the adapter has no
        upward dependency."""
        try:
            import logging

            import yfinance as yf

            logging.getLogger("yfinance").setLevel(logging.CRITICAL)
            tk = yf.Ticker(code)
            p = getattr(getattr(tk, "fast_info", None), "last_price", None)
            if not p:
                hist = tk.history(period="1d")
                if not hist.empty:
                    p = float(hist["Close"].iloc[-1])
            return Decimal(str(p)) if p else None
        except Exception:  # noqa: BLE001
            return None

    def _price_implausible_for_stock(self, code: str, price: Decimal) -> bool:
        """True if ``price`` is too small to be a share price for ``code`` — the
        signature of an option premium misreported as a stock fill.

        Fail-safe: any quote failure (or non-positive price) returns False, so a
        network hiccup never drops a real holding."""
        if price <= 0:
            return False
        spot = self._live_price(code)
        if not spot or spot <= 0:
            return False
        return price < spot * self._STOCK_PRICE_FLOOR_FRAC

    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]:
        trades: list[StockTrade] = []
        for order, fill_dt in self._get_filled_orders(since):
            qty = dec(order.executed_quantity)
            if qty == 0:
                continue
            code = str(order.symbol).split(".")[0]  # Longbridge uses AAPL.US
            price = dec(order.executed_price or order.price or "0")
            if is_option_code(code) or self._has_option_fees(order):
                continue  # option execution — handled by fetch_option_executions

            # Longbridge occasionally reports an option/combo close under the bare
            # underlying symbol with an EMPTY charge_detail — indistinguishable
            # from a stock fill by symbol or fees (a real stock fill can be
            # zero-fee too). The tell is price: an option premium is a small
            # fraction of the share price. Only when a fill has no fee data at all
            # AND its price is implausibly low vs the live quote do we treat it as
            # a misreported option and drop it (Longbridge exposes no
            # options-position API to rebuild the legs from).
            if not self._has_fee_data(order) and self._price_implausible_for_stock(code, price):
                continue

            trades.append(
                StockTrade(
                    date=self._timestamp_to_date(fill_dt.timestamp()),
                    broker=Broker.LONGBRIDGE,
                    ticker=code,
                    action=StockAction.BUY if order.side == OrderSide.Buy else StockAction.SELL,
                    qty=qty,
                    price=price,
                    fee=self._fee(order),
                    currency=str(order.currency),
                    fill_id=str(order.order_id),
                    timestamp=fill_dt,
                )
            )
        return trades

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]:
        """Option executions carry an OCC-style symbol (e.g.
        ``PYPL260828C60000.US``) or combo symbol (e.g. ``SNDQ260821P23/25``).
        Route those here and decompose multi-leg combos into individual leg trades."""
        trades: list[OptionTrade] = []
        for order, fill_dt in self._get_filled_orders(since):
            qty = dec(order.executed_quantity)
            if qty == 0:
                continue
            code = str(order.symbol).split(".")[0]
            legs = parse_option_legs(code)

            if legs is None:
                if not self._has_option_fees(order):
                    continue  # stock — handled by fetch_stock_executions

                # Combo order returned with parent underlying symbol (e.g. CRWV.US)
                # Resolve legs from positions matching this underlying
                matching_positions = [
                    p for p in self.fetch_positions()
                    if p.asset_type == AssetType.OPTION and p.symbol == code
                ]
                if matching_positions:
                    fee = self._fee(order)
                    for i, pos in enumerate(matching_positions):
                        leg_is_buy = (pos.qty > 0)
                        leg_fee = fee if i == 0 else Decimal("0")
                        trades.append(
                            OptionTrade(
                                date=self._timestamp_to_date(fill_dt.timestamp()),
                                broker=Broker.LONGBRIDGE,
                                underlying=pos.symbol,
                                option_type=pos.option_type,
                                strike=pos.strike,
                                qty=abs(pos.qty),
                                expiry=pos.expiry,
                                action=OptionAction.BUY if leg_is_buy else OptionAction.SELL,
                                premium=pos.avg_cost or Decimal("0"),
                                fee=leg_fee,
                                currency=str(order.currency),
                                fill_id=f"{order.order_id}:{i}",
                                timestamp=fill_dt,
                                strategy="Vertical Spread" if len(matching_positions) == 2 else "Multi-Leg",
                            )
                        )
                continue

            is_buy = (order.side == OrderSide.Buy)
            price = dec(order.executed_price or order.price or "0")
            fee = self._fee(order)

            if len(legs) == 1:
                underlying, otype, strike, expiry = legs[0]
                trades.append(
                    OptionTrade(
                        date=self._timestamp_to_date(fill_dt.timestamp()),
                        broker=Broker.LONGBRIDGE,
                        underlying=underlying,
                        option_type=otype,
                        strike=strike,
                        qty=qty,
                        expiry=expiry,
                        action=OptionAction.BUY if is_buy else OptionAction.SELL,
                        premium=price,
                        fee=fee,
                        currency=str(order.currency),
                        fill_id=str(order.order_id),
                        timestamp=fill_dt,
                    )
                )
            else:
                buy_flags = _combo_leg_buy_flags(legs, is_buy)
                for i, (underlying, otype, strike, expiry) in enumerate(legs):
                    leg_is_buy = buy_flags[i]
                    leg_fee = fee if i == 0 else Decimal("0")
                    trades.append(
                        OptionTrade(
                            date=self._timestamp_to_date(fill_dt.timestamp()),
                            broker=Broker.LONGBRIDGE,
                            underlying=underlying,
                            option_type=otype,
                            strike=strike,
                            qty=qty,
                            expiry=expiry,
                            action=OptionAction.BUY if leg_is_buy else OptionAction.SELL,
                            premium=price if i == 0 else Decimal("0"),
                            fee=leg_fee,
                            currency=str(order.currency),
                            fill_id=f"{order.order_id}:{i}",
                            timestamp=fill_dt,
                            strategy="Vertical Spread" if len(legs) == 2 else "Multi-Leg",
                        )
                    )
        return trades

    def _get_filled_orders(self, since: date | None) -> list:
        """Discover filled orders by FILL time via history_executions and
        today_executions, then join each unique order_id to order_detail for
        full detail + fee. Returns ``(OrderDetail, fill_datetime)`` tuples,
        dated by the fill's trade_done_at (the latest fill for a partially-filled
        order)."""
        start_at = self._since_to_datetime(since)
        executions = list(self._call_with_retry(self._client.history_executions, start_at=start_at) or [])
        try:
            today_execs = self._call_with_retry(self._client.today_executions) or []
            for te in today_execs:
                te_dt = getattr(te, "trade_done_at", None)
                if te_dt is not None and start_at is not None:
                    # Ensure timezone-aware comparison
                    if te_dt.tzinfo is None and start_at.tzinfo is not None:
                        te_dt = te_dt.replace(tzinfo=start_at.tzinfo)
                    if te_dt < start_at:
                        continue
                executions.append(te)
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).debug("Longbridge today_executions error: %s", exc)

        # Unique order_ids, remembering each order's latest fill time.
        order_ids: list = []
        fill_dt: dict = {}
        for ex in executions:
            oid = ex.order_id
            done_at = ex.trade_done_at
            if oid not in fill_dt:
                order_ids.append(oid)
                fill_dt[oid] = done_at
            elif done_at > fill_dt[oid]:
                fill_dt[oid] = done_at

        out: list = []
        for oid in order_ids:
            detail = self._order_detail(oid)
            if detail is None:
                continue
            out.append((detail, fill_dt[oid]))
        return out

    # -- positions (seeding + reconciliation, §5/§9) ------------------------ #
    def fetch_positions(self) -> list[Position]:
        positions: list[Position] = []
        
        result = self._call_with_retry(self._client.stock_positions)
        if not result or not result.channels:
            return positions
            
        today = datetime.now(tz=self._tz).date()
        for channel in result.channels:
            for pos in channel.positions:
                qty = dec(pos.quantity)
                if qty == 0:
                    continue

                code = str(pos.symbol).split(".")[0]
                legs = parse_option_legs(code)
                common = dict(
                    broker=Broker.LONGBRIDGE,
                    qty=qty,
                    avg_cost=dec(pos.cost_price),
                    currency=str(pos.currency),
                    market_price=None,  # SDK doesn't return market_price on the position
                    as_of=today,
                )
                if legs is None:
                    name = str(getattr(pos, "symbol_name", "") or "")
                    positions.append(Position(asset_type=AssetType.STOCK, symbol=code, name=name, **common))
                else:
                    for underlying, otype, strike, expiry in legs:
                        positions.append(Position(
                            asset_type=AssetType.OPTION, symbol=underlying,
                            option_type=otype, strike=strike, expiry=expiry, **common,
                        ))
        return positions

    # -- account value (§4 dashboard) --------------------------------------- #
    def fetch_account_value(self) -> list[tuple[Decimal, str]]:
        """Net asset value per currency balance, as (amount, currency)."""
        out: list[tuple[Decimal, str]] = []
        try:
            balances = self._call_with_retry(self._client.account_balance)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: Longbridge account_balance failed: {e}")
            return out
        for b in (balances or []):
            na = getattr(b, "net_assets", None)
            if na is not None:
                out.append((dec(na), str(getattr(b, "currency", "SGD") or "SGD")))
        return out

    def fetch_cash_balances(self) -> list[tuple[Decimal, str]]:
        """Free uninvested cash / settled buying power as (amount, currency)."""
        out: list[tuple[Decimal, str]] = []
        try:
            balances = self._call_with_retry(self._client.account_balance)
        except Exception as e:  # noqa: BLE001
            print(f"Warning: Longbridge account_balance failed: {e}")
            return out
        for b in (balances or []):
            cash_amt = getattr(b, "buy_power", None)
            if cash_amt is None:
                cash_amt = getattr(b, "cash", None)
            if cash_amt is None:
                cash_amt = getattr(b, "settled_cash", None)
            if cash_amt is not None and float(cash_amt) > 0:
                out.append((dec(cash_amt), str(getattr(b, "currency", "SGD") or "SGD")))
        return out

    # -- cash movements (§8, best-effort per §14) --------------------------- #
    def fetch_cash_movements(self, since: date | None) -> list[CashMovement]:
        if not self._cash_enabled:
            return []
            
        start_at = self._since_to_datetime(since)
        # We need a start_at and end_at for cash_flow, but longport might require it
        # Let's provide a default wide range if since is None
        if start_at is None:
            start_at = datetime(2000, 1, 1, tzinfo=self._tz)
            
        end_at = datetime.now(tz=self._tz)
        
        cash_flows = self._call_with_retry(self._client.cash_flow, start_at=start_at, end_at=end_at)
        if not cash_flows:
            return []

        movements: list[CashMovement] = []
        for cf in cash_flows:
            amount = dec(cf.balance)
            if amount == 0:
                continue
                
            # Human-readable note from the flow name + description (the
            # business_type is an opaque enum like "BalanceType.Unknown", so we
            # don't surface it).
            note_parts = []
            flow_name = str(cf.transaction_flow_name or "").strip()
            if flow_name and flow_name.lower() != "none":
                note_parts.append(flow_name)
            if cf.description:
                desc_str = str(cf.description).strip()
                if desc_str and desc_str.lower() != "none":
                    note_parts.append(desc_str)

            # Classify from the transaction flow name / description.
            name = str(cf.transaction_flow_name).upper()
            desc = str(cf.description).upper()

            cash_type = CashType.INTERNAL_TRANSFER
            if "DEPOSIT" in name or "DEPOSIT" in desc:
                cash_type = CashType.DEPOSIT
            elif "WITHDRAW" in name or "WITHDRAW" in desc:
                cash_type = CashType.WITHDRAWAL
            elif "DIVIDEND" in name or "DIVIDEND" in desc:
                cash_type = CashType.DIVIDEND
            elif "FEE" in name or "FEE" in desc or "INTEREST" in name:
                cash_type = CashType.FEE
            elif "CONVERSION" in name or "EXCHANGE" in name:
                cash_type = CashType.FX_CONVERSION

            movements.append(
                CashMovement(
                    date=self._timestamp_to_date(cf.business_time.timestamp()),
                    broker=Broker.LONGBRIDGE,
                    type=cash_type,
                    amount=abs(amount),
                    currency=str(cf.currency),
                    note="; ".join(note_parts),
                    fill_id=None,
                )
            )
            
        return movements

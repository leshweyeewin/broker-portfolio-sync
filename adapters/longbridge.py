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
)

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

    # -- executions --------------------------------------------------------- #
    def _order_detail(self, order_id: str):
        """Fetch an order's full detail (fields + charge_detail), with a
        rate-limit retry. Returns None if it can't be read."""
        import time
        for attempt in range(3):
            try:
                return self._client.order_detail(order_id)
            except Exception as e:  # noqa: BLE001
                if "429" in str(e) and attempt < 2:
                    time.sleep(2)  # wait for rate limit to reset
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

    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]:
        trades: list[StockTrade] = []
        for order, fill_dt in self._get_filled_orders(since):
            qty = dec(order.executed_quantity)
            if qty == 0:
                continue
            code = str(order.symbol).split(".")[0]  # Longbridge uses AAPL.US
            if is_option_code(code):
                continue  # option execution — handled by fetch_option_executions

            trades.append(
                StockTrade(
                    date=self._timestamp_to_date(fill_dt.timestamp()),
                    broker=Broker.LONGBRIDGE,
                    ticker=code,
                    action=StockAction.BUY if order.side == OrderSide.Buy else StockAction.SELL,
                    qty=qty,
                    price=dec(order.executed_price or order.price or "0"),
                    fee=self._fee(order),
                    currency=str(order.currency),
                    fill_id=str(order.order_id),
                    timestamp=fill_dt,
                )
            )
        return trades

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]:
        """Option executions carry an OCC-style symbol (e.g.
        ``PYPL260828C60000.US``). Route those here so they land in the Options
        tab, not Stocks."""
        trades: list[OptionTrade] = []
        for order, fill_dt in self._get_filled_orders(since):
            qty = dec(order.executed_quantity)
            if qty == 0:
                continue
            code = str(order.symbol).split(".")[0]
            parsed = parse_option_code(code)
            if parsed is None:
                continue  # stock — handled by fetch_stock_executions
            underlying, otype, strike, expiry = parsed

            trades.append(
                OptionTrade(
                    date=self._timestamp_to_date(fill_dt.timestamp()),
                    broker=Broker.LONGBRIDGE,
                    underlying=underlying,
                    option_type=otype,
                    strike=strike,
                    qty=qty,
                    expiry=expiry,
                    action=OptionAction.BUY if order.side == OrderSide.Buy else OptionAction.SELL,
                    premium=dec(order.executed_price or order.price or "0"),
                    fee=self._fee(order),
                    currency=str(order.currency),
                    fill_id=str(order.order_id),
                    timestamp=fill_dt,
                )
            )
        return trades

    def _get_filled_orders(self, since: date | None) -> list:
        """Discover filled orders by FILL time via history_executions, then join
        each unique order_id to order_detail for full detail + fee. Returns
        ``(OrderDetail, fill_datetime)`` tuples, dated by the fill's
        trade_done_at (the latest fill for a partially-filled order)."""
        start_at = self._since_to_datetime(since)
        executions = self._client.history_executions(start_at=start_at) or []

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
        
        result = self._client.stock_positions()
        if not result or not result.channels:
            return positions
            
        today = datetime.now(tz=self._tz).date()
        for channel in result.channels:
            for pos in channel.positions:
                qty = dec(pos.quantity)
                if qty == 0:
                    continue

                code = str(pos.symbol).split(".")[0]
                opt = parse_option_code(code)
                common = dict(
                    broker=Broker.LONGBRIDGE,
                    qty=qty,
                    avg_cost=dec(pos.cost_price),
                    currency=str(pos.currency),
                    market_price=None,  # SDK doesn't return market_price on the position
                    as_of=today,
                )
                if opt is None:
                    positions.append(Position(asset_type=AssetType.STOCK, symbol=code, **common))
                else:
                    underlying, otype, strike, expiry = opt
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
            balances = self._client.account_balance()
        except Exception as e:  # noqa: BLE001
            print(f"Warning: Longbridge account_balance failed: {e}")
            return out
        for b in (balances or []):
            na = getattr(b, "net_assets", None)
            if na is not None:
                out.append((dec(na), str(getattr(b, "currency", "SGD") or "SGD")))
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
        
        cash_flows = self._client.cash_flow(start_at=start_at, end_at=end_at)
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

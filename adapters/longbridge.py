"""Longbridge adapter (step 6 of the build order — BUILD_SPEC.md §3).

Conforms to the :class:~adapters.base.BrokerAdapter protocol.

Auth: App Key + App Secret + Access Token (OpenAPI).
Field mapping:
- Executions: history_orders filtered for Filled/PartialFilled. We use orders
  instead of history_executions because we need the fee, which is retrieved
  via order_detail(order.order_id).charge_detail.total_amount.
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
    OrderStatus,
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
    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]:
        orders = self._get_filled_orders(since)
        trades: list[StockTrade] = []
        for order in orders:
            qty = dec(order.executed_quantity)
            if qty == 0:
                continue
                
            # Fetch fee from order details (with rate limit retry)
            import time
            fee = Decimal("0")
            for attempt in range(3):
                try:
                    detail = self._client.order_detail(order.order_id)
                    if detail and detail.charge_detail:
                        fee = dec(detail.charge_detail.total_amount or "0")
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < 2:
                        time.sleep(2)  # Wait for rate limit to reset
                    else:
                        print(f"Warning: Could not fetch fee for {order.symbol} (ID: {order.order_id}): {e}")
                        break

            trades.append(
                StockTrade(
                    date=self._timestamp_to_date(order.updated_at.timestamp()),
                    broker=Broker.LONGBRIDGE,
                    ticker=str(order.symbol).split(".")[0], # Longbridge uses AAPL.US
                    action=StockAction.BUY if order.side == OrderSide.Buy else StockAction.SELL,
                    qty=qty,
                    price=dec(order.executed_price or order.price or "0"),
                    fee=fee,
                    currency=str(order.currency),
                    fill_id=str(order.order_id),
                )
            )
        return trades

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]:
        # Longbridge currently does not provide an options API via openapi SDK 
        # (OptionPosition doesn't exist, options are managed via mobile app mostly).
        # We return empty for now unless options are supported in the future.
        return []

    def _get_filled_orders(self, since: date | None) -> list:
        start_at = self._since_to_datetime(since)
        # Fetch all orders (history_orders) and filter
        orders = self._client.history_orders(start_at=start_at)
        if not orders:
            return []
            
        filled_orders = []
        for order in orders:
            if order.status in (OrderStatus.Filled, OrderStatus.PartialFilled):
                filled_orders.append(order)
        return filled_orders

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
                    
                positions.append(
                    Position(
                        broker=Broker.LONGBRIDGE,
                        asset_type=AssetType.STOCK,
                        symbol=str(pos.symbol).split(".")[0],
                        qty=qty,
                        avg_cost=dec(pos.cost_price),
                        currency=str(pos.currency),
                        market_price=None, # SDK doesn't return market_price directly in position
                        as_of=today,
                    )
                )
        return positions

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

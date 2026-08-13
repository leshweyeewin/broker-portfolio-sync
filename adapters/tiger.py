"""Tiger Brokers adapter (step 2 of the build order — BUILD_SPEC.md §12).

Chosen first because it's the easiest real API (no gateway sidecar, plain RSA
auth) and so validates the common schema end-to-end. It conforms to the
:class:`~adapters.base.BrokerAdapter` protocol and returns only the normalized
dataclasses from :mod:`adapters.base`.

Auth (§3): RSA private key + ``tiger_id`` + ``account``, with ``license='TBSG'``
for the Singapore account. Register at developer.itigerup.com. The pipeline only
*reads* these from the secret store; setting them up is the user's job (§13).

Field mapping was derived against the installed ``tigeropen`` SDK surface:

* Executions come from ``TradeClient.get_filled_orders`` — one ``Order`` per
  filled execution. Order-level (rather than tick-level ``get_transactions``)
  is deliberate: the fill-level endpoint carries no commission, and §8 requires
  a real Fee per row. An order carries ``avg_fill_price``, ``filled``, and
  ``commission`` together, which is exactly one execution row.
* Positions come from ``get_positions`` (STK + OPT) for seeding (§5) and
  reconciliation (§9): ``quantity`` + ``average_cost``.
* Cash movements come from ``get_fund_details``. Tiger's fund-detail schema
  varies by account, so this is best-effort with explicit classification; per
  §14 the user can disable it (``cash_movements_enabled=False``) and hand-enter
  cash flows if the history is too thin.

Assumptions surfaced (per the spec's "surface the assumption" instruction):

* Option ``multiplier`` defaults to the contract's own value, else 100 (§14).
* Option ``average_cost`` from a Tiger position is treated as premium-per-share;
  confirm against a live SG account (§14 open item on premium basis).
* Realized P/L is *not* filled in here — the FIFO engine (§12 step 3) computes
  it downstream. The adapter only reports executions and positions.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from tigeropen.common.consts import (
    Currency,
    Language,
    Market,
    SecurityType,
    SegmentType,
)
from tigeropen.common.util.signature_utils import read_private_key
from tigeropen.tiger_open_config import TigerOpenClientConfig
from tigeropen.trade.trade_client import TradeClient

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

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Normalization tables
# --------------------------------------------------------------------------- #
# Tiger fund-detail "type" strings -> canonical CashType (§8). Unmapped types
# are kept (as INTERNAL_TRANSFER, tagged with the raw type) rather than dropped,
# so nothing silently vanishes and nothing unknown inflates Net Capital In.
_FUND_TYPE_MAP: dict[str, CashType] = {
    "DEPOSIT": CashType.DEPOSIT,
    "RECHARGE": CashType.DEPOSIT,
    "CASH_IN": CashType.DEPOSIT,
    "IN": CashType.DEPOSIT,
    "WITHDRAWAL": CashType.WITHDRAWAL,
    "WITHDRAW": CashType.WITHDRAWAL,
    "CASH_OUT": CashType.WITHDRAWAL,
    "OUT": CashType.WITHDRAWAL,
    "DIVIDEND": CashType.DIVIDEND,
    "DVD": CashType.DIVIDEND,
    "CASH_DIVIDEND": CashType.DIVIDEND,
    "FEE": CashType.FEE,
    "COMMISSION": CashType.FEE,
    "INTEREST": CashType.FEE,
    "INT": CashType.FEE,
    "TRANSFER": CashType.INTERNAL_TRANSFER,
    "INTERNAL_TRANSFER": CashType.INTERNAL_TRANSFER,
    "FX": CashType.FX_CONVERSION,
    "EXCHANGE": CashType.FX_CONVERSION,
    "CURRENCY_EXCHANGE": CashType.FX_CONVERSION,
}

# Candidate column names in the get_fund_details DataFrame (schema varies).
_AMOUNT_COLS = ("amount", "change_amount", "cash_change", "cash_amount", "value")
_CURRENCY_COLS = ("currency", "ccy")
_TYPE_COLS = ("fund_type", "type", "category", "biz_type")
_DATE_COLS = ("settled_time", "created_at", "timestamp", "time", "date", "trade_time")
_ID_COLS = ("id", "fund_id", "transaction_id", "serial_no")
_NOTE_COLS = ("remark", "note", "description", "memo")


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@dataclass
class TigerCredentials:
    """Tiger auth inputs, read from the secret store — never hard-coded (§9).

    ``private_key`` accepts either the PEM contents (how Secret Manager stores
    it) or a filesystem path to the key file.
    """

    tiger_id: str
    account: str
    private_key: str
    license: str = "TBSG"  # Singapore account (§3)
    secret_key: str = ""  # institutional/prime only; usually blank for SG
    timezone: str = "Asia/Singapore"
    sandbox: bool = False

    @classmethod
    def from_env(cls, prefix: str = "TIGER_") -> "TigerCredentials":
        """Build from environment variables (``TIGER_ID``, ``TIGER_ACCOUNT``,
        ``TIGER_PRIVATE_KEY``, ``TIGER_LICENSE``, ``TIGER_SECRET_KEY``,
        ``TIGER_TIMEZONE``)."""

        def _req(name: str) -> str:
            val = os.environ.get(prefix + name)
            if not val:
                raise ValueError(f"missing required env var {prefix + name}")
            return val

        return cls(
            tiger_id=_req("ID"),
            account=_req("ACCOUNT"),
            private_key=_req("PRIVATE_KEY"),
            license=os.environ.get(prefix + "LICENSE", "TBSG"),
            secret_key=os.environ.get(prefix + "SECRET_KEY", ""),
            timezone=os.environ.get(prefix + "TIMEZONE", "Asia/Singapore"),
        )


def _resolve_private_key(private_key: str) -> str:
    """Return PEM contents whether given inline PEM, a path to a .pem key file,
    or a path to a tiger_openapi_config.properties file.
    """
    if "-----BEGIN" in private_key:
        return private_key

    content = private_key
    if os.path.exists(private_key):
        content = Path(private_key).read_text(encoding="utf-8")
        if "-----BEGIN" in content:
            return content

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("private_key_pk1="):
            pk1 = line.split("=", 1)[1].strip()
            return f"-----BEGIN RSA PRIVATE KEY-----\n{pk1}\n-----END RSA PRIVATE KEY-----"
        if line.startswith("private_key_pk8="):
            pk8 = line.split("=", 1)[1].strip()
            return f"-----BEGIN PRIVATE KEY-----\n{pk8}\n-----END PRIVATE KEY-----"

    if os.path.exists(private_key):
        return read_private_key(private_key)

    raise ValueError(
        "TigerCredentials.private_key is neither inline PEM nor an existing "
        "file path; provide the RSA private key contents or a valid path"
    )


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class TigerAdapter:
    """Tiger implementation of :class:`~adapters.base.BrokerAdapter`."""

    name: str = Broker.TIGER.value

    def __init__(
        self,
        credentials: Optional[TigerCredentials] = None,
        *,
        client: Optional[TradeClient] = None,
        timezone: Optional[str] = None,
        cash_movements_enabled: bool = True,
    ) -> None:
        """Either pass ``credentials`` (the normal path) or inject a ready
        ``client`` (for tests). ``cash_movements_enabled=False`` skips the
        fund-detail fetch entirely so cash can be hand-entered (§14)."""
        if client is None and credentials is None:
            raise ValueError("provide either credentials or a client")

        tz_name = timezone or (credentials.timezone if credentials else "Asia/Singapore")
        self._tz = ZoneInfo(tz_name)
        self._cash_enabled = cash_movements_enabled
        self._account_ids_cache: Optional[list] = None

        if client is not None:
            self._client = client
        else:
            assert credentials is not None
            self._client = self._build_client(credentials)

    # -- accounts ----------------------------------------------------------- #
    def _account_ids(self) -> list:
        """Real (non-paper) Tiger account ids to query — a login can hold several
        (e.g. a margin account plus a CASH 'boost' account). ``[None]`` falls back
        to the client's default account when the list can't be determined."""
        if self._account_ids_cache is None:
            ids: list = []
            try:
                for a in (self._client.get_managed_accounts() or []):
                    if str(getattr(a, "account_type", "")).upper() == "PAPER":
                        continue
                    acct = getattr(a, "account", None)
                    if acct:
                        ids.append(str(acct))
            except Exception as exc:  # noqa: BLE001 — fall back to default account
                log.warning("Tiger get_managed_accounts failed: %s", exc)
            self._account_ids_cache = ids or [None]
        return self._account_ids_cache

    # -- client construction ------------------------------------------------ #
    @staticmethod
    def _build_client(creds: TigerCredentials) -> TradeClient:
        config = TigerOpenClientConfig(sandbox_debug=creds.sandbox)
        config.private_key = _resolve_private_key(creds.private_key)
        config.tiger_id = creds.tiger_id
        config.account = creds.account
        config.language = Language.en_US
        config.timezone = creds.timezone
        if creds.license:
            config.license = creds.license
        if creds.secret_key:
            config.secret_key = creds.secret_key
        return TradeClient(config)

    # -- time helpers ------------------------------------------------------- #
    def _since_to_ms(self, since: date | None) -> int:
        """Local-midnight of ``since`` as epoch milliseconds (Tiger's start_time
        unit). Defaults to 2020-01-01 if None to satisfy API requirement."""
        if since is None:
            since = date(2020, 1, 1)
        start = datetime(since.year, since.month, since.day, tzinfo=self._tz)
        return int(start.timestamp() * 1000)

    def _epoch_ms_to_date(self, ms: object) -> date:
        """Convert a Tiger epoch-ms timestamp to a local calendar date."""
        return datetime.fromtimestamp(int(ms) / 1000, tz=self._tz).date()

    # -- executions --------------------------------------------------------- #
    def fetch_stock_executions(self, since: date | None) -> list[StockTrade]:
        orders = self._get_filled_orders(SecurityType.STK, since)
        trades: list[StockTrade] = []
        for order in orders:
            qty = self._filled_qty(order)
            if qty == 0:
                continue  # not actually an execution
            action = self._stock_action(order)
            contract = order.contract
            trades.append(
                StockTrade(
                    date=self._order_date(order),
                    broker=Broker.TIGER,
                    ticker=self._symbol(contract),
                    action=action,
                    qty=qty,
                    price=dec(order.avg_fill_price),
                    fee=self._order_fee(order),
                    currency=self._currency(contract),
                    fill_id=self._order_id(order),
                    timestamp=self._order_datetime(order),
                )
            )
        return trades

    def fetch_option_executions(self, since: date | None) -> list[OptionTrade]:
        trades: list[OptionTrade] = []

        # Single-leg option orders.
        for order in self._get_filled_orders(SecurityType.OPT, since):
            qty = self._filled_qty(order)
            if qty == 0:
                continue
            contract = order.contract
            trades.append(
                OptionTrade(
                    date=self._order_date(order),
                    broker=Broker.TIGER,
                    underlying=self._symbol(contract),
                    option_type=self._option_type(contract),
                    strike=dec(contract.strike),
                    qty=qty,
                    expiry=self._parse_expiry(contract.expiry),
                    action=self._option_action(order),
                    premium=dec(order.avg_fill_price),
                    fee=self._order_fee(order),
                    currency=self._currency(contract),
                    multiplier=self._multiplier(contract),
                    fill_id=self._order_id(order),
                    timestamp=self._order_datetime(order),
                )
            )

        # Multi-leg combo orders (verticals, calendars, diagonals, rolls). Tiger
        # returns these under sec_type MLEG with a single net contract; the actual
        # legs (each with its own action/strike/expiry/price) live on
        # ``order.contract_legs``. Decompose each into per-leg option trades so
        # spreads/rolls show individually and reconcile against positions.
        for order in self._get_filled_orders(SecurityType.MLEG, since):
            trades.extend(self._decompose_mleg(order))

        return trades

    def _decompose_mleg(self, order) -> list[OptionTrade]:
        legs = getattr(order, "contract_legs", None) or []
        oid = self._order_id(order)
        order_date = self._order_date(order)
        ts = self._order_datetime(order)
        # The order fee is charged once for the whole combo — attribute it to the
        # first leg so Total Fees isn't multiplied across legs.
        order_fee = self._order_fee(order)
        out: list[OptionTrade] = []
        for i, leg in enumerate(legs):
            qty = dec(getattr(leg, "filled_quantity", 0) or getattr(leg, "total_quantity", 0) or 0)
            if qty == 0:
                continue
            right = str(getattr(leg, "put_call", "")).strip().upper()
            otype = OptionType.CALL if right.startswith("C") else OptionType.PUT
            action = (
                OptionAction.BUY
                if str(getattr(leg, "action", "")).strip().upper() == "BUY"
                else OptionAction.SELL
            )
            out.append(
                OptionTrade(
                    date=order_date,
                    broker=Broker.TIGER,
                    underlying=str(leg.symbol).strip(),
                    option_type=otype,
                    strike=dec(leg.strike),
                    qty=qty,
                    expiry=self._parse_expiry(leg.expiry),
                    action=action,
                    premium=dec(getattr(leg, "avg_filled_price", 0) or 0),
                    fee=order_fee if i == 0 else Decimal("0"),
                    currency=str(getattr(leg, "currency", "") or "USD"),
                    multiplier=dec(getattr(leg, "multiplier", 100) or 100),
                    # Unique, stable per leg so re-runs upsert rather than collide.
                    fill_id=f"{oid}:{i}" if oid else None,
                    timestamp=ts,
                )
            )
        return out

    def _get_filled_orders(self, sec_type: SecurityType, since: date | None) -> list:
        """Fetch filled orders of a security type since a date, chunking into <=89-day windows."""
        from datetime import timedelta
        today = datetime.now(tz=self._tz).date()
        start_date = since if since else date(today.year - 1, today.month, today.day)

        all_orders = []
        for account in self._account_ids():
            cur_start = start_date
            while cur_start <= today:
                cur_end = min(cur_start + timedelta(days=89), today)
                start_ms = int(datetime(cur_start.year, cur_start.month, cur_start.day, tzinfo=self._tz).timestamp() * 1000)
                end_ms = int(datetime(cur_end.year, cur_end.month, cur_end.day, 23, 59, 59, tzinfo=self._tz).timestamp() * 1000)

                res = self._client.get_filled_orders(
                    account=account,
                    sec_type=sec_type,
                    market=Market.ALL,
                    start_time=start_ms,
                    end_time=end_ms,
                    limit=1000,
                )
                if res:
                    all_orders.extend(list(res))
                cur_start = cur_end + timedelta(days=1)

        seen_ids = set()
        unique_orders = []
        for order in all_orders:
            oid = self._order_id(order)
            if oid:
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)
            unique_orders.append(order)

        return unique_orders

    # -- positions (seeding + reconciliation, §5/§9) ------------------------ #
    def fetch_positions(self) -> list[Position]:
        positions: list[Position] = []
        positions.extend(self._fetch_positions(SecurityType.STK))
        positions.extend(self._fetch_positions(SecurityType.OPT))
        return positions

    def _fetch_positions(self, sec_type: SecurityType) -> list[Position]:
        result = []
        for account in self._account_ids():
            res = self._client.get_positions(
                account=account, sec_type=sec_type, currency=Currency.ALL, market=Market.ALL
            )
            if res:
                result.extend(list(res))
        if not result:
            return []
        today = datetime.now(tz=self._tz).date()
        out: list[Position] = []
        for pos in result:
            contract = pos.contract
            qty = dec(pos.quantity)
            if qty == 0:
                continue
            is_option = sec_type is SecurityType.OPT
            out.append(
                Position(
                    broker=Broker.TIGER,
                    asset_type=AssetType.OPTION if is_option else AssetType.STOCK,
                    symbol=self._symbol(contract),
                    qty=qty,
                    avg_cost=dec(pos.average_cost),
                    currency=self._currency(contract),
                    market_price=(
                        dec(pos.market_price) if pos.market_price is not None else None
                    ),
                    as_of=today,
                    option_type=self._option_type(contract) if is_option else None,
                    strike=(
                        dec(contract.strike)
                        if is_option and contract.strike is not None
                        else None
                    ),
                    expiry=(
                        self._parse_expiry(contract.expiry) if is_option else None
                    ),
                    multiplier=self._multiplier(contract) if is_option else Decimal("100"),
                )
            )
        return out

    # -- cash movements (§8, best-effort per §14) --------------------------- #
    def fetch_cash_movements(self, since: date | None) -> list[CashMovement]:
        """Cash flows from two distinct Tiger endpoints (§8):

        * ``get_fund_details`` — fees, dividends, interest (no real deposits).
        * ``get_funding_history`` — actual deposits and withdrawals.

        Each source is fetched independently so an empty or unavailable one
        never suppresses the other.
        """
        if not self._cash_enabled:
            return []
        movements = self._fund_detail_movements(since)
        movements += self._funding_history_movements()
        return movements

    def _fund_detail_movements(self, since: date | None) -> list[CashMovement]:
        """Fees / dividends / interest from ``get_fund_details``."""
        df = self._client.get_fund_details(
            seg_types=[SegmentType.SEC],
            start_date=since.isoformat() if since else None,
        )
        if df is None or getattr(df, "empty", True):
            return []

        cols = {c.lower(): c for c in df.columns}
        amount_col = self._pick_col(cols, _AMOUNT_COLS)
        currency_col = self._pick_col(cols, _CURRENCY_COLS)
        type_col = self._pick_col(cols, _TYPE_COLS)
        date_col = self._pick_col(cols, _DATE_COLS)
        # Fail loud (§9) rather than mis-map an unrecognized schema.
        missing = [
            name
            for name, col in (
                ("amount", amount_col),
                ("currency", currency_col),
                ("type", type_col),
                ("date", date_col),
            )
            if col is None
        ]
        if missing:
            raise ValueError(
                "Tiger fund-details schema is missing expected column(s): "
                f"{missing}; available columns: {list(df.columns)}. Inspect the "
                "schema and extend the column maps, or construct the adapter "
                "with cash_movements_enabled=False and hand-enter cash flows."
            )
        id_col = self._pick_col(cols, _ID_COLS)
        note_col = self._pick_col(cols, _NOTE_COLS)

        movements: list[CashMovement] = []
        for _, row in df.iterrows():
            raw_type = str(row[type_col]).strip()
            cash_type = _FUND_TYPE_MAP.get(raw_type.upper())
            note_parts = []
            if cash_type is None:
                # Unknown type: keep it, tagged, out of external-capital math.
                cash_type = CashType.INTERNAL_TRANSFER
                note_parts.append(f"unmapped fund_type={raw_type}")
            elif raw_type:
                note_parts.append(raw_type)

            # Deposits/withdrawals come authoritatively from get_funding_history.
            # fund_details classification of them is unreliable (observed: an
            # ambiguous row surfacing as a spurious $1,000 deposit), so never let
            # this endpoint feed external capital into the Transactions tab.
            if cash_type.is_external_capital:
                continue

            if note_col is not None and row.get(note_col):
                note_parts.append(str(row[note_col]))

            movements.append(
                CashMovement(
                    date=self._cash_date(row[date_col]),
                    broker=Broker.TIGER,
                    type=cash_type,
                    amount=abs(dec(row[amount_col])),  # Transactions tab is positive
                    currency=str(row[currency_col]),
                    note="; ".join(note_parts),
                    fill_id=str(row[id_col]) if id_col is not None else None,
                )
            )
        return movements

    def _funding_history_movements(self) -> list[CashMovement]:
        """Deposits / withdrawals from ``get_funding_history``.

        Best-effort (§14): not every account can read this endpoint, so a
        failure is logged and skipped rather than aborting the cash fetch.
        """
        try:
            funding_df = self._client.get_funding_history()
        except Exception as exc:
            log.warning("Could not fetch Tiger funding history: %s", exc)
            return []
        if funding_df is None or getattr(funding_df, "empty", True):
            return []

        movements: list[CashMovement] = []
        for _, row in funding_df.iterrows():
            raw_type = str(row.get("type_desc", "")).strip().lower()
            if raw_type == "deposit":
                cash_type = CashType.DEPOSIT
            elif raw_type == "withdraw":
                cash_type = CashType.WITHDRAWAL
            else:
                continue

            movements.append(
                CashMovement(
                    date=self._cash_date(row["created_at"]),
                    broker=Broker.TIGER,
                    type=cash_type,
                    amount=abs(dec(row["amount"])),
                    currency=str(row["currency"]),
                    note=f"Tiger funding {raw_type}",
                    fill_id=str(row["id"]) if "id" in row else None,
                )
            )
        return movements

    # -- small field helpers ------------------------------------------------ #
    @staticmethod
    def _pick_col(cols: dict[str, str], candidates: Iterable[str]) -> Optional[str]:
        for cand in candidates:
            if cand in cols:
                return cols[cand]
        return None

    @staticmethod
    def _symbol(contract) -> str:
        return str(contract.symbol).strip()

    @staticmethod
    def _currency(contract) -> str:
        return str(contract.currency or "").strip()

    @staticmethod
    def _order_id(order) -> Optional[str]:
        oid = getattr(order, "id", None) or getattr(order, "order_id", None)
        return str(oid) if oid is not None else None

    @staticmethod
    def _filled_qty(order) -> Decimal:
        filled = getattr(order, "filled", None)
        if filled is None:
            filled = getattr(order, "quantity", 0)
        return dec(filled if filled is not None else 0)

    def _order_date(self, order) -> date:
        ts = getattr(order, "trade_time", None) or getattr(order, "order_time", None)
        if ts is None:
            raise ValueError(f"Tiger order {self._order_id(order)} has no trade_time")
        return self._epoch_ms_to_date(ts)

    def _order_datetime(self, order) -> Optional[datetime]:
        """Full execution timestamp (tz-aware), for datetime-precise incrementals."""
        ts = getattr(order, "trade_time", None) or getattr(order, "order_time", None)
        if ts is None:
            return None
        return datetime.fromtimestamp(int(ts) / 1000, tz=self._tz)

    @staticmethod
    def _order_fee(order) -> Decimal:
        """Consolidate commission + GST into one Fee per row (§8)."""
        fee = Decimal("0")
        for attr in ("commission", "gst"):
            val = getattr(order, attr, None)
            if val is not None:
                fee += dec(val)
        return fee

    @staticmethod
    def _stock_action(order) -> StockAction:
        return StockAction.BUY if TigerAdapter._is_buy(order) else StockAction.SELL

    @staticmethod
    def _option_action(order) -> OptionAction:
        return OptionAction.BUY if TigerAdapter._is_buy(order) else OptionAction.SELL

    @staticmethod
    def _is_buy(order) -> bool:
        action = getattr(order, "action", "")
        return "BUY" in str(action).upper()

    @staticmethod
    def _option_type(contract) -> OptionType:
        pc = str(getattr(contract, "put_call", "") or "").upper()
        if pc.startswith("C"):
            return OptionType.CALL
        if pc.startswith("P"):
            return OptionType.PUT
        raise ValueError(f"unrecognized option put_call={pc!r} on {contract.symbol}")

    @staticmethod
    def _multiplier(contract) -> Decimal:
        mult = getattr(contract, "multiplier", None)
        return dec(mult) if mult else Decimal("100")

    def _cash_date(self, value: object) -> date:
        # Fund-detail dates may be epoch ms or a date string.
        if isinstance(value, (int, float)):
            return self._epoch_ms_to_date(value)
        return self._parse_expiry(value)

    @staticmethod
    def _parse_expiry(value: object) -> date:
        """Parse Tiger date fields: epoch-ms int, 'YYYYMMDD', or 'YYYY-MM-DD'."""
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, (int, float)):
            # Heuristic: large numbers are epoch ms, else treat as YYYYMMDD int.
            ival = int(value)
            if ival > 10_000_000_00:  # > ~ year 2001 in ms
                return datetime.utcfromtimestamp(ival / 1000).date()
            value = str(ival)
        s = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"cannot parse Tiger date value {value!r}")

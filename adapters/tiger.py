"""Tiger Brokers adapter (step 2 of the build order — BUILD_SPEC.md §12).

Chosen first because it's the easiest real API (no gateway sidecar, plain RSA
auth) and so validates the common schema end-to-end. It conforms to the
:class:`~adapters.base.BrokerAdapter` protocol and returns only the normalized
dataclasses from :mod:`adapters.base`.

Auth (§3): RSA private key + ``tiger_id`` + ``account``, with ``license='TBSG'``
for the Singapore account. Register at developer.itigerup.com. The pipeline only
*reads* these from the secret store; setting them up is the user's job (§13).

Field mapping was derived against the installed ``tigeropen`` SDK surface:

* Executions are discovered by FILL time via ``TradeClient.get_transactions``
  (fill-level, filtered by when a fill happened) and then enriched to full
  ``Order`` detail + commission via ``get_order``. This deliberately does NOT
  use ``get_filled_orders`` for discovery: that endpoint filters by when an
  order was *placed*, so a resting order placed before the window but filled
  inside it is silently missed. The fill records carry no fee (§8 needs a real
  Fee per row), so we join each fill's ``order_id`` back to ``get_order`` —
  which also yields ``avg_fill_price``, ``filled``, and ``contract_legs`` for
  combos, giving exactly one execution row (or one per combo leg).
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
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
        # get_funding_history() is scoped to the configured account, so its
        # deposits/withdrawals belong to this account (Tiger exposes no per-account
        # attribution beyond it). Surfaced in the movement note.
        self._funding_account: Optional[str] = credentials.account if credentials else None

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

        # Both single-leg option orders and multi-leg combos (verticals,
        # calendars, diagonals, rolls) surface under sec_type OPT: each combo leg
        # is a fill sharing the parent combo's order_id, so get_order returns the
        # combo with its legs on ``order.contract_legs``. Branch per order: a
        # combo (legs present) is decomposed into per-leg trades so spreads/rolls
        # show individually and reconcile against positions; a single-leg order
        # is one trade.
        for order in self._get_filled_orders(SecurityType.OPT, since):
            if getattr(order, "contract_legs", None):
                trades.extend(self._decompose_mleg(order))
                continue
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
        """Return filled ``Order`` objects discovered by FILL time.

        Discovery has two sources:

        1. ``get_transactions`` (fill-level: filtered by *when a fill happened*),
           not ``get_filled_orders`` (which filters by *when an order was
           placed*). A resting order placed before ``since`` but filled inside
           the window is therefore captured, not silently dropped.
        2. Corporate-action settlements — option exercise/assignment and share
           call-away — which Tiger books as filled orders tagged
           ``source == 'asset-task'`` but produces **no transaction** for, so
           step 1 never sees them. Without these an expired ITM option would
           never close and would reconcile as "missing from broker" forever.

        Detail (fee, combo legs) is joined from the bulk ``get_filled_orders``
        cache, falling back to per-order ``get_order`` for anything the cache
        missed. run.py drops anything dated on/before the cutoff by its own date."""
        today = datetime.now(tz=self._tz).date()
        start_ms = self._since_to_ms(since)
        end_ms = int(
            datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=self._tz).timestamp() * 1000
        )

        orders: list = []
        for account in self._account_ids():
            # Detail cache: get_filled_orders returns up to 1000 full orders
            # (fee + combo legs) per call and is not per-order rate-limited, so a
            # wide placement scan is a handful of calls. It also carries the
            # asset-task settlements used below.
            detail = self._bulk_order_detail(account, sec_type, today)

            include: list = []  # order_ids to build, in discovery order
            seen: set = set()

            # 1) Market fills, by FILL time (the completeness source of truth).
            for txn in self._iter_transactions(account, sec_type, start_ms, end_ms):
                oid = getattr(txn, "order_id", None)
                if oid is None:
                    continue
                key = str(oid)
                if key not in seen:
                    seen.add(key)
                    include.append(key)

            # 2) Corporate-action settlements (exercise/assignment/call-away):
            #    filled orders with no transaction, so add them from the cache.
            for key, o in detail.items():
                if key in seen:
                    continue
                if str(getattr(o, "source", "")).lower() == "asset-task":
                    seen.add(key)
                    include.append(key)

            for key in include:
                order = detail.get(key) or self._get_order_detail(account, int(key))
                if order is not None:
                    orders.append(order)
        return orders

    def _bulk_order_detail(self, account, sec_type: SecurityType, today: date) -> dict:
        """Map ``order_id`` -> full ``Order`` for a wide placement window, chunked
        into <=89-day windows (get_filled_orders spans are bounded). For OPT we
        also scan MLEG, because combos surface at order level under MLEG even
        though their fills surface under OPT."""
        sec_types = [sec_type]
        if sec_type == SecurityType.OPT:
            sec_types.append(SecurityType.MLEG)
        start_date = today - timedelta(days=400)
        detail: dict = {}
        cur_start = start_date
        while cur_start <= today:
            cur_end = min(cur_start + timedelta(days=89), today)
            s_ms = int(datetime(cur_start.year, cur_start.month, cur_start.day, tzinfo=self._tz).timestamp() * 1000)
            e_ms = int(datetime(cur_end.year, cur_end.month, cur_end.day, 23, 59, 59, tzinfo=self._tz).timestamp() * 1000)
            for stt in sec_types:
                res = self._client.get_filled_orders(
                    account=account, sec_type=stt, market=Market.ALL,
                    start_time=s_ms, end_time=e_ms, limit=1000,
                )
                for o in (res or []):
                    oid = self._order_id(o)
                    if oid:
                        detail[oid] = o
            cur_start = cur_end + timedelta(days=1)
        return detail

    def _iter_transactions(self, account, sec_type: SecurityType, start_ms: int, end_ms: int):
        """Yield fill records in ``[start_ms, end_ms]``, paginating past the
        100-row server cap. Passing ``page_token`` (even '') makes the SDK return
        a ``TransactionsResponse`` (``.result`` + ``.next_page_token``) rather than
        a bare, capped list."""
        token = ""
        while True:
            resp = self._client.get_transactions(
                account=account,
                sec_type=sec_type,
                start_time=start_ms,
                end_time=end_ms,
                limit=100,
                page_token=token,
            )
            if resp is None:
                return
            yield from (getattr(resp, "result", None) or [])
            token = getattr(resp, "next_page_token", None)
            if not token:
                return

    def _get_order_detail(self, account, order_id):
        """Fetch a full ``Order`` (contract, legs, avg_fill_price, commission) by
        id, retrying briefly so a transient rate-limit never silently drops a
        fill — the whole point of the fill-time switch is to miss nothing."""
        for attempt in range(3):
            try:
                return self._client.get_order(
                    account=account, id=int(order_id), show_charges=True
                )
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log.warning("Tiger get_order failed for %s: %s", order_id, exc)
                    return None
                time.sleep(0.5 * (attempt + 1))
        return None

    # -- positions (seeding + reconciliation, §5/§9) ------------------------ #
    def fetch_positions(self) -> list[Position]:
        positions: list[Position] = []
        positions.extend(self._fetch_positions(SecurityType.STK))
        positions.extend(self._fetch_positions(SecurityType.OPT))
        return positions

    # -- account value (§4 dashboard) --------------------------------------- #
    def fetch_account_value(self) -> list[tuple[Decimal, str]]:
        """Net liquidation value per live account, as (amount, currency).

        Summed across both Tiger accounts (margin + cash/boost); the caller
        converts to SGD. Best-effort: an account that can't be read is skipped.
        """
        out: list[tuple[Decimal, str]] = []
        for account in self._account_ids():
            try:
                res = (self._client.get_assets(account=account)
                       if account is not None else self._client.get_assets())
            except Exception as exc:  # noqa: BLE001
                log.warning("Tiger get_assets failed for %s: %s", account, exc)
                continue
            for a in (res or []):
                s = getattr(a, "summary", None)
                nl = getattr(s, "net_liquidation", None)
                if nl is not None:
                    out.append((dec(nl), str(getattr(s, "currency", "USD") or "USD")))
        return out

    def fetch_cash_balances(self) -> list[tuple[Decimal, str]]:
        """Free uninvested cash / available buying power per live account, as (amount, currency)."""
        import math
        out: list[tuple[Decimal, str]] = []
        for account in self._account_ids():
            try:
                res = (self._client.get_assets(account=account)
                       if account is not None else self._client.get_assets())
            except Exception as exc:  # noqa: BLE001
                log.warning("Tiger get_assets for cash failed for %s: %s", account, exc)
                continue
            for a in (res or []):
                val = None
                seg = getattr(a, "segments", None)
                if seg and "S" in seg:
                    s_seg = seg["S"]
                    val = getattr(s_seg, "available_funds", None)
                    if val is None or math.isinf(float(val)) or float(val) < 0:
                        val = getattr(s_seg, "excess_liquidity", None)
                if val is None or math.isinf(float(val)):
                    s = getattr(a, "summary", None)
                    if s:
                        val = getattr(s, "buying_power", None)
                        if val is None or math.isinf(float(val)):
                            val = getattr(s, "cash", None)
                if val is not None and not math.isinf(float(val)) and float(val) > 0:
                    out.append((dec(val), "USD"))
        return out

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
                    name=("" if is_option else str(getattr(contract, "name", "") or "")),
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
        """Deposits / withdrawals from ``get_funding_history``, per account.

        ``get_funding_history`` takes no account argument — it is scoped to the
        client's configured account and returns a *disjoint* set of records per
        account. So a login with a margin + cash account has funding in BOTH, and
        querying only the configured one silently drops the other's deposits. We
        loop every real account (temporarily pointing the client at each) and tag
        each movement with its account. Best-effort (§14): a per-account failure
        is logged and skipped rather than aborting the whole cash fetch."""
        movements: list[CashMovement] = []
        for account in self._account_ids():
            movements.extend(self._funding_for_account(account))
        return movements

    def _funding_for_account(self, account: Optional[str]) -> list[CashMovement]:
        cfg = getattr(self._client, "_TigerOpenClient__config", None)
        prev_account = getattr(self._client, "_account", None)
        prev_cfg_account = getattr(cfg, "account", None) if cfg is not None else None
        try:
            if account is not None:
                self._client._account = account
                if cfg is not None:
                    cfg.account = account
            funding_df = self._client.get_funding_history()
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch Tiger funding history for %s: %s", account, exc)
            return []
        finally:
            if account is not None:
                if prev_account is not None:
                    self._client._account = prev_account
                if cfg is not None and prev_cfg_account is not None:
                    cfg.account = prev_cfg_account

        if funding_df is None or getattr(funding_df, "empty", True):
            return []

        label = account or self._funding_account
        movements: list[CashMovement] = []
        for _, row in funding_df.iterrows():
            raw_type = str(row.get("type_desc", "")).strip().lower()
            if raw_type == "deposit":
                cash_type = CashType.DEPOSIT
            elif raw_type == "withdraw":
                cash_type = CashType.WITHDRAWAL
            else:
                continue

            note = f"Tiger funding {raw_type}"
            if label:
                note += f" · Acct {label}"
            movements.append(
                CashMovement(
                    date=self._cash_date(row["created_at"]),
                    broker=Broker.TIGER,
                    type=cash_type,
                    amount=abs(dec(row["amount"])),
                    currency=str(row["currency"]),
                    note=note,
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

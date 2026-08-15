"""run.py — the daily sync entrypoint (step 9 — BUILD_SPEC.md §9, §12).

One unattended pass:

    fetch (per broker) -> [seed] -> compute FIFO -> FX -> write (idempotent)
    -> reconcile -> Run Log -> alert on anything non-clean.

Two layers, for testability:

* ``run_sync(adapters, writer, fx, ...)`` is pure orchestration over injected
  collaborators (adapters, a ``PortfolioWriter``, an ``FxRates``, a notifier
  callable). It touches no global config and no network of its own, so the whole
  pipeline is unit-tested offline with fakes (see ``tests/test_run.py``).
* ``main()`` wires the *real* collaborators from ``config.settings`` and env,
  then calls ``run_sync``. It's the container's ``CMD``.

Fault model (§9): each broker is fetched independently. If one broker's API
fails, its leg is dropped from *this* run (both trades and positions, so
reconciliation doesn't false-flag it) and the run is marked ``PARTIAL`` with the
error in the Run Log and an alert — the other brokers still sync. Only if *every*
broker fails is the run ``FAILED``.

Seeding (``--seed``): on the very first run, pass ``--seed`` to synthesize
Opening Balance rows from current positions (stable dedup keys, so re-running
``--seed`` upserts rather than duplicates). Thereafter, forward runs (no
``--seed``) LOAD those persisted opening balances back from the sheet into FIFO
and only apply fills dated after the seed — so holdings reconcile and realized
P/L on seeded positions is correct without re-pulling full history (§5/§14).
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from adapters.base import (
    BrokerAdapter,
    CashMovement,
    OptionAction,
    OptionTrade,
    Position,
    StockAction,
    StockTrade,
)
from core.fifo_pl import FifoResult, compute_option_pl, compute_stock_pl
from core.fx import FxRates
from core.reconcile import reconcile, seed_positions, expire_worthless_options
from sheets.writer import (
    DASHBOARD_HEADERS,
    build_option_row,
    build_run_log_row,
    build_stock_row,
    build_transaction_row,
)

log = logging.getLogger("run")

ZERO = Decimal("0")

# A broker leg that blocks longer than this is treated as failed for this run
# (§9 fail-soft). Guards against a broker SDK with no timeout of its own — e.g. a
# wedged MooMoo OpenD gateway, which once hung a whole run for 6 hours. Override
# via env for a one-time full-history bootstrap (Tiger's multi-year pull is slow).
BROKER_FETCH_TIMEOUT_S = float(os.environ.get("BROKER_FETCH_TIMEOUT_S", "180"))

# Over-fetch this far before the opening-balance cutoff so a broker's coarse,
# timezone-shifted `since` filter can't drop a boundary fill that is actually
# after the cutoff on our (SGT) calendar. The pre-cutoff overshoot is dropped
# after fetch by our own trade dates. 4 days safely spans a weekend + TZ skew.
#
# NB: a broker whose `since` filters by *order-placement* time (Tiger) needs to
# reach much further back to catch a resting order filled inside the window —
# that lookback is handled inside the adapter, not by widening this buffer, which
# would make other brokers (MooMoo) miss recent fills.
_FETCH_BUFFER = timedelta(days=4)

# A notifier takes a message and returns True if it was delivered. Injected so
# tests can assert on alerts; main() uses alerting.notify.notify_safe.
Notifier = Callable[[str], bool]


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #
@dataclass
class BrokerData:
    """Everything fetched from one broker in a run (or the error that stopped it)."""

    broker: str
    stocks: list[StockTrade] = field(default_factory=list)
    options: list[OptionTrade] = field(default_factory=list)
    cash: list[CashMovement] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    # Current net-liquidation value per account, as (amount, currency).
    account_value: list[tuple[Decimal, str]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RunResult:
    """Outcome of one ``run_sync`` pass — mirrors the Run Log row."""

    status: str  # "OK" | "PARTIAL" | "FAILED"
    stocks_added: int = 0
    stocks_updated: int = 0
    options_added: int = 0
    options_updated: int = 0
    transactions_added: int = 0
    warnings: list[str] = field(default_factory=list)
    reconciliation: str = "OK"
    fx_rates_used: str = ""
    alerted: bool = False


# --------------------------------------------------------------------------- #
# Fetch — one broker, fail-soft
# --------------------------------------------------------------------------- #
def _safe_account_value(adapter: BrokerAdapter) -> list[tuple[Decimal, str]]:
    """Fetch an adapter's account value if it supports it, guarded so a failure
    (or an adapter without the method) never sinks the broker's whole fetch."""
    fn = getattr(adapter, "fetch_account_value", None)
    if fn is None:
        return []
    try:
        return list(fn())
    except Exception:  # noqa: BLE001
        log.warning("Account-value fetch failed for %s", adapter.name, exc_info=True)
        return []


def collect_broker_data(
    adapter: BrokerAdapter,
    since: Optional[date],
    timeout_s: float = BROKER_FETCH_TIMEOUT_S,
) -> BrokerData:
    """Fetch a single broker's executions/cash/positions.

    Fail-soft (§9): any exception *or* a hang past ``timeout_s`` is captured on
    ``BrokerData.error`` so one broker's outage can't sink the others — including
    a broker SDK that blocks forever with no timeout of its own. The fetch runs
    on a daemon thread, so a wedged call can never block process exit; if it
    times out we abandon that leg and mark the run PARTIAL.
    """
    box: dict[str, BrokerData] = {}

    def _fetch() -> None:
        try:
            box["data"] = BrokerData(
                broker=adapter.name,
                stocks=list(adapter.fetch_stock_executions(since)),
                options=list(adapter.fetch_option_executions(since)),
                cash=list(adapter.fetch_cash_movements(since)),
                positions=list(adapter.fetch_positions()),
                account_value=_safe_account_value(adapter),
            )
        except Exception as exc:  # noqa: BLE001 — deliberately broad; recorded, not swallowed
            log.exception("Broker %s fetch failed", adapter.name)
            box["data"] = BrokerData(broker=adapter.name, error=f"{type(exc).__name__}: {exc}")
        finally:
            # Release broker resources (e.g. MooMoo OpenD contexts) so we don't
            # leak connections across runs; best-effort.
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.warning("Broker %s close() failed", adapter.name, exc_info=True)

    worker = threading.Thread(target=_fetch, name=f"fetch-{adapter.name}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        log.error("Broker %s fetch timed out after %.0fs — skipping this leg",
                  adapter.name, timeout_s)
        return BrokerData(broker=adapter.name, error=f"timed out after {timeout_s:.0f}s")
    return box["data"]


# --------------------------------------------------------------------------- #
# Row building (FIFO realized P/L + FX join)
# --------------------------------------------------------------------------- #
def _realized_sgd_by_key(result: FifoResult, fx: FxRates) -> dict[str, Decimal]:
    """Map each closing execution's dedup_key -> realized P/L converted to SGD
    at the *trade-date* rate (so historical rows never drift, §7)."""
    return {
        r.key: fx.to_sgd(r.realized_pl, r.currency, on=r.date)
        for r in result.realizations
    }


def _stock_rows(stocks: list[StockTrade], result: FifoResult, get_sgd: Callable[[str], Optional[Decimal]]) -> list[list[Any]]:
    by_key = result.realized_by_key
    rows = []
    for t in stocks:
        r = by_key.get(t.dedup_key)
        is_closed = (t.dedup_key in result.fully_closed_keys) or (r is not None)
        status_str = "Closed" if is_closed else "Open"
        if r is not None:
            rows.append(
                build_stock_row(
                    t, status=status_str,
                    realized_pl=r.realized_pl,
                    realized_pl_sgd=get_sgd(t.dedup_key),
                )
            )
        else:
            rows.append(build_stock_row(t, status=status_str))
    return rows


def _option_rows(options: list[OptionTrade], result: FifoResult, get_sgd: Callable[[str], Optional[Decimal]]) -> list[list[Any]]:
    by_key = result.realized_by_key
    rows = []
    for t in options:
        r = by_key.get(t.dedup_key)
        is_closed = (t.dedup_key in result.fully_closed_keys) or (r is not None)
        status_str = "Closed" if is_closed else "Open"
        if r is not None:
            rows.append(
                build_option_row(
                    t, status=status_str,
                    realized_pl=r.realized_pl,
                    realized_pl_sgd=get_sgd(t.dedup_key),
                )
            )
        else:
            rows.append(build_option_row(t, status=status_str))
    return rows


def _transaction_rows(cash: Sequence[CashMovement], fx: FxRates) -> list[list[Any]]:
    """Only external deposits/withdrawals land in the Transactions tab (§8)."""
    rows = []
    for cm in cash:
        if not cm.type.is_external_capital:
            continue
        rows.append(build_transaction_row(cm, fx.to_sgd(cm.amount, cm.currency, on=cm.date)))
    return rows


def _build_dashboard(
    status: str,
    holdings: Sequence,
    reconciliation: str,
    *,
    account_value_sgd: Optional[dict[str, Decimal]] = None,
    net_capital_in: Optional[dict[str, Decimal]] = None,
    weekly_realized_sgd_by_broker: Optional[dict[str, Decimal]] = None,
    monthly_realized_sgd_by_broker: Optional[dict[str, Decimal]] = None,
    ytd_realized_sgd_by_broker: Optional[dict[str, Decimal]] = None,
) -> list[list[Any]]:
    """Machine-computed summary block for the Dashboard tab (§4).

    Per broker (+ SGD total):
      * Net Capital In (SGD)   = Σ deposits − Σ withdrawals (from Transactions)
      * Account Value (SGD)    = live net-liquidation value
      * Total P/L (SGD)        = Account Value − Net Capital In
      * Realized P/L over three rolling windows (Week / Month / Year),
        Open Positions, and run health.

    Total P/L is the all-in gain (realized + unrealized + dividends − fees) vs the
    money actually put in. A broker whose deposits can't be pulled (MooMoo) shows
    Net Capital In 0 until they're hand-entered, so its Total P/L is blank rather
    than a misleading figure.
    """
    account_value_sgd = account_value_sgd or {}
    net_capital_in = net_capital_in or {}
    brokers = DASHBOARD_HEADERS[1:-1]  # ["Longbridge", "Tiger", "MooMoo"]

    def _row(label: str, by_broker: dict[str, Decimal]) -> tuple[list[Any], Decimal]:
        row: list[Any] = [label]
        total = ZERO
        for b in brokers:
            v = by_broker.get(b, ZERO)
            row.append(float(v))
            total += v
        row.append(float(total))
        return row, total

    capital_row, capital_total = _row("Net Capital In (SGD)", net_capital_in)
    value_row, value_total = _row("Account Value (SGD)", account_value_sgd)

    # Total P/L = value − capital, per broker; blank where capital is unknown
    # (no deposits recorded but the broker holds value, e.g. MooMoo).
    pl_row: list[Any] = ["Total P/L (SGD)"]
    pl_total = ZERO
    for b in brokers:
        val = account_value_sgd.get(b)
        cap = net_capital_in.get(b)
        if val is None or (cap is None and val is None):
            pl_row.append("")
            continue
        val = val or ZERO
        if cap is None and (val != ZERO):
            pl_row.append("")  # holds value but capital-in unknown -> can't compute
            continue
        pl = val - (cap or ZERO)
        pl_row.append(float(pl))
        pl_total += pl
    pl_row.append(float(pl_total))

    week_row, _ = _row("This Week Realized (SGD)", weekly_realized_sgd_by_broker or {})
    month_row, _ = _row("This Month Realized (SGD)", monthly_realized_sgd_by_broker or {})
    ytd_row, _ = _row("This Year Realized (SGD)", ytd_realized_sgd_by_broker or {})

    counts = Counter(h.broker.value for h in holdings)
    open_row: list[Any] = ["Open Positions"]
    for b in brokers:
        open_row.append(counts.get(b, 0))
    open_row.append(sum(counts.values()))

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        DASHBOARD_HEADERS,
        capital_row,
        value_row,
        pl_row,
        week_row,
        month_row,
        ytd_row,
        open_row,
        ["Status", status, "", "", ""],
        ["Reconciliation", reconciliation, "", "", ""],
        ["Last Run (UTC)", ts, "", "", ""],
    ]


# --------------------------------------------------------------------------- #
# Seeding — opening balance as (current position − forward journal)
# --------------------------------------------------------------------------- #
def _stock_instrument(t: StockTrade) -> tuple:
    return (t.broker, t.ticker)


def _option_instrument(t: OptionTrade) -> tuple:
    return (t.broker, t.underlying, t.option_type, t.strike, t.expiry)


def _backout_openings(openings: list, journal: list, seed_date: date, key) -> list:
    """Seed Opening-Balance rows representing the position *before* the forward
    journal, over the union of current positions and journaled instruments.

    The position-derived seed already includes the journaled fills, so keeping
    both would double-count. The pre-journal quantity is:

        opening_qty = current_qty − Σ(signed journal qty)   (acquisitions +, disposals −)

    computed for every instrument in *either* the seed *or* the journal. This
    reconstructs holdings back to the broker exactly for every instrument —
    including one that was fully **closed** during the journal window (current
    qty 0, so it has no position-derived seed row, but its closing fill still
    needs a lot to close against). Rows that net to zero are dropped.

    Cost basis on the opening lot:
    * instrument still held  → broker position avg cost (from the seed row);
    * instrument now flat     → VWAP of its journaled fills (best available proxy).
    Either way this is an approximation for realized P/L on a journaled sell of a
    pre-seed lot — the true pre-seed basis isn't fetchable (§5, documented).
    """
    net: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    jtrades: dict[tuple, list] = defaultdict(list)
    for t in journal:
        net[key(t)] += t.qty if t.action.is_acquisition else -t.qty
        jtrades[key(t)].append(t)

    ob_by_key = {key(ob): ob for ob in openings}

    adjusted: list = []
    for k in ob_by_key.keys() | net.keys():
        cur = ob_by_key[k].qty if k in ob_by_key else ZERO
        qty = cur - net.get(k, ZERO)
        if qty == 0:
            continue
        if k in ob_by_key:
            adjusted.append(_reseed_qty(ob_by_key[k], qty, seed_date))
        else:
            adjusted.append(_synth_opening(jtrades[k], qty, seed_date))
    return adjusted


def _vwap(trades: list) -> Decimal:
    """Volume-weighted average price/premium over |qty| of ``trades``."""
    num = sum((t.qty * _unit_price(t) for t in trades), ZERO)
    den = sum((t.qty for t in trades), ZERO)
    return num / den if den else _unit_price(trades[0])


def _unit_price(t) -> Decimal:
    return t.price if isinstance(t, StockTrade) else t.premium


def _reseed_qty(ob, qty: Decimal, seed_date: date):
    """Rebuild a position-derived Opening-Balance row with an adjusted qty."""
    if isinstance(ob, StockTrade):
        return StockTrade(
            date=seed_date, broker=ob.broker, ticker=ob.ticker,
            action=StockAction.OPENING_BALANCE, qty=qty, price=ob.price,
            fee=0, currency=ob.currency,
        )
    return OptionTrade(
        date=seed_date, broker=ob.broker, underlying=ob.underlying,
        option_type=ob.option_type, strike=ob.strike, qty=qty, expiry=ob.expiry,
        action=OptionAction.OPENING_BALANCE, premium=ob.premium, fee=0,
        currency=ob.currency, multiplier=ob.multiplier,
    )


def _synth_opening(journal: list, qty: Decimal, seed_date: date):
    """Synthesize an Opening-Balance row for an instrument that is in the journal
    but not in current positions (fully closed during the window), priced at the
    journal VWAP so its close reconciles with ≈0 realized P/L."""
    t0 = journal[0]
    price = _vwap(journal)
    if isinstance(t0, StockTrade):
        return StockTrade(
            date=seed_date, broker=t0.broker, ticker=t0.ticker,
            action=StockAction.OPENING_BALANCE, qty=qty, price=price,
            fee=0, currency=t0.currency,
        )
    return OptionTrade(
        date=seed_date, broker=t0.broker, underlying=t0.underlying,
        option_type=t0.option_type, strike=t0.strike, qty=qty, expiry=t0.expiry,
        action=OptionAction.OPENING_BALANCE, premium=price, fee=0,
        currency=t0.currency, multiplier=t0.multiplier,
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_sync(
    adapters: Sequence[BrokerAdapter],
    writer,
    fx: FxRates,
    *,
    today: Optional[date] = None,
    since: Optional[date] = None,
    seed: bool = False,
    notifier: Optional[Notifier] = None,
) -> RunResult:
    """Run one full sync pass over ``adapters`` into ``writer``. See module docs."""
    today = today or date.today()

    # 1. Establish the opening-balance CUTOFF (fills on/before it belong to the
    #    opening balance; fills after it are the forward journal) and the fetch
    #    window. On a forward run the cutoff comes from the persisted Opening
    #    Balances; on a seed it's the day before --since. Both modes then fetch and
    #    drop against the SAME cutoff, so a daily run reproduces the seed's journal
    #    exactly (idempotent, no drift).
    ob_stocks: list[StockTrade] = []
    ob_options: list[OptionTrade] = []
    seed_date: Optional[date] = None
    if seed:
        seed_date = (since - timedelta(days=1)) if since else today
        cutoff: Optional[date] = seed_date
    else:
        ob_stocks, ob_options = writer.read_opening_balances()
        cutoff = max((t.date for t in (ob_stocks + ob_options)), default=None)

    # Fetch a few days BEFORE the cutoff: a broker's `since` filter is coarse and
    # applied in the broker's own timezone, so an early-in-the-day fill that is
    # after the cutoff on our (SGT) calendar can fall before it on theirs and get
    # dropped. We over-fetch, then drop the pre-cutoff overshoot by our own date.
    fetch_since = since
    if cutoff is not None:
        fetch_since = cutoff - _FETCH_BUFFER

    # 2. Fetch every broker (fail-soft per broker).
    datas = [collect_broker_data(a, fetch_since) for a in adapters]
    fetch_errors = [(d.broker, d.error) for d in datas if d.error]
    ok = [d for d in datas if d.error is None]

    stocks = [t for d in ok for t in d.stocks]
    options = [t for d in ok for t in d.options]
    cash = [c for d in ok for c in d.cash]
    positions = [p for d in ok for p in d.positions]

    # 3. Drop fills on/before the cutoff (baked into the opening balance) using our
    #    own trade dates, keeping the forward journal. Then establish the opening
    #    balances: --seed synthesizes them from live positions with the journal
    #    backed out (so the seed isn't double-counted); a forward run reuses the
    #    persisted ones loaded above.
    if cutoff is not None:
        stocks = [t for t in stocks if t.date > cutoff]
        options = [t for t in options if t.date > cutoff]
    if seed:
        ob_stocks, ob_options = seed_positions(positions, seed_date)
        ob_stocks = _backout_openings(ob_stocks, stocks, seed_date, _stock_instrument)
        ob_options = _backout_openings(ob_options, options, seed_date, _option_instrument)
    stocks = ob_stocks + stocks
    options = ob_options + options

    # 3. FIFO realized P/L + remaining holdings.
    stock_result = compute_stock_pl(stocks)
    option_result = compute_option_pl(options)

    # Close options that expired out-of-the-money: the broker drops them with no
    # settlement fill, so realize them at premium 0 and re-run FIFO so the P/L and
    # flattened holdings are correct. (ITM expiries close via real
    # assignment/exercise fills upstream and are already flat here.)
    expiry_closes = expire_worthless_options(option_result.holdings, positions, today)
    if expiry_closes:
        options = options + expiry_closes
        option_result = compute_option_pl(options)

    # 4. Build rows (FX-converted, realized P/L joined onto closing rows).
    realized_sgd = {**_realized_sgd_by_key(stock_result, fx), **_realized_sgd_by_key(option_result, fx)}
    stock_rows = _stock_rows(stocks, stock_result, realized_sgd.get)
    option_rows = _option_rows(options, option_result, realized_sgd.get)
    txn_rows = _transaction_rows(cash, fx)

    # 5. Write idempotently.
    writer.ensure_tabs()
    s_res = writer.upsert_stocks(stock_rows)
    o_res = writer.upsert_options(option_rows)
    t_res = writer.upsert_transactions(txn_rows)

    # Re-apply formatting now that data rows exist: appended rows inherit the
    # header's fill/text, so this resets them to the theme default. Then sort
    # each tab chronologically (appends land at the bottom).
    writer.apply_formatting()
    writer.sort_data_tabs()

    # 6. Reconcile computed holdings against broker-reported positions.
    holdings = list(stock_result.holdings) + list(option_result.holdings)
    recon_warnings = reconcile(holdings, positions)

    # 7. Dashboard summary: realized P/L, net capital in, current value, total P/L.
    # Realized P/L per broker (SGD) over three rolling windows, all on the SGT
    # calendar: this week (ISO Monday..today), this month (1st..today) and this
    # year (Jan 1..today). The weekly line mirrors the Sunday P/L digest.
    monday = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    weekly_realized_sgd_by_broker: dict[str, Decimal] = defaultdict(lambda: ZERO)
    monthly_realized_sgd_by_broker: dict[str, Decimal] = defaultdict(lambda: ZERO)
    ytd_realized_sgd_by_broker: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in list(stock_result.realizations) + list(option_result.realizations):
        amt = realized_sgd.get(r.key, ZERO)
        if r.date >= year_start:
            ytd_realized_sgd_by_broker[r.broker.value] += amt
        if r.date >= month_start:
            monthly_realized_sgd_by_broker[r.broker.value] += amt
        if r.date >= monday:
            weekly_realized_sgd_by_broker[r.broker.value] += amt

    # Current account value per broker in SGD (live rate — this is a snapshot, not
    # a historical row, so it uses the current FX, not a trade-date rate).
    account_value_sgd: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for d in ok:
        for amount, ccy in d.account_value:
            try:
                account_value_sgd[d.broker] += fx.current_to_sgd(amount, ccy)
            except Exception:  # noqa: BLE001 — FX outage shouldn't sink the run
                log.warning("Could not convert %s %s to SGD for %s dashboard",
                            amount, ccy, d.broker, exc_info=True)

    # Net external capital per broker (Σ deposits − Σ withdrawals, SGD) from the
    # full Transactions history on the sheet.
    net_capital_in = writer.read_net_capital_in_by_broker()

    # 8. Status + warnings.
    if not ok:
        status = "FAILED"
    elif fetch_errors:
        status = "PARTIAL"
    else:
        status = "OK"

    warnings = [f"[{broker}] fetch failed: {err}" for broker, err in fetch_errors]
    warnings.extend(recon_warnings)
    reconciliation = "OK" if not recon_warnings else f"{len(recon_warnings)} mismatch(es)"

    writer.overwrite_dashboard(
        _build_dashboard(
            status, holdings, reconciliation,
            account_value_sgd=dict(account_value_sgd),
            net_capital_in=net_capital_in,
            weekly_realized_sgd_by_broker=dict(weekly_realized_sgd_by_broker),
            monthly_realized_sgd_by_broker=dict(monthly_realized_sgd_by_broker),
            ytd_realized_sgd_by_broker=dict(ytd_realized_sgd_by_broker),
        )
    )

    # 9. Run Log.
    fx_used = ", ".join(f"{p}={r}" for p, r in sorted(fx.cached_pairs_for_date(today).items()))
    writer.append_run_log(
        build_run_log_row(
            status=status,
            stocks_added=s_res.added,
            stocks_updated=s_res.updated,
            options_added=o_res.added,
            options_updated=o_res.updated,
            transactions_added=t_res.added,
            fx_rates_used=fx_used,
            reconciliation=reconciliation,
            warnings=" | ".join(warnings),
        )
    )

    # 10. Alert on anything non-clean.
    alerted = False
    if notifier is not None and (status != "OK" or recon_warnings):
        alerted = notifier(_format_alert(status, reconciliation, warnings, today))

    return RunResult(
        status=status,
        stocks_added=s_res.added,
        stocks_updated=s_res.updated,
        options_added=o_res.added,
        options_updated=o_res.updated,
        transactions_added=t_res.added,
        warnings=warnings,
        reconciliation=reconciliation,
        fx_rates_used=fx_used,
        alerted=alerted,
    )


def _format_alert(status: str, reconciliation: str, warnings: Sequence[str], today: date) -> str:
    lines = [
        f"broker-portfolio-sync {status} for {today.isoformat()}",
        f"Reconciliation: {reconciliation}",
    ]
    if warnings:
        lines.append("")
        lines.extend(f"• {w}" for w in warnings)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main() — wire the real collaborators
# --------------------------------------------------------------------------- #
def _build_adapters() -> list[BrokerAdapter]:
    """Instantiate every broker adapter whose credentials are present.

    A broker with missing/invalid config is skipped with a warning rather than
    blocking the others — the same fail-soft spirit as the fetch loop.
    """
    from config.settings import ConfigError

    adapters: list[BrokerAdapter] = []

    def _try(name: str, build: Callable[[], BrokerAdapter]) -> None:
        try:
            adapters.append(build())
        except (ConfigError, ValueError, KeyError) as exc:
            log.warning("Skipping %s adapter — not configured: %s", name, exc)

    def _tiger() -> BrokerAdapter:
        from adapters.tiger import TigerAdapter, TigerCredentials
        return TigerAdapter(TigerCredentials.from_env())

    def _longbridge() -> BrokerAdapter:
        from adapters.longbridge import LongbridgeAdapter, LongbridgeCredentials
        return LongbridgeAdapter(LongbridgeCredentials.from_env())

    def _moomoo() -> BrokerAdapter:
        import socket
        from adapters.moomoo import MooMooAdapter, MooMooCredentials
        creds = MooMooCredentials.from_env()
        if "MOOMOO_HOST" not in os.environ:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            try:
                sock.connect((creds.host, creds.port))
                sock.close()
            except OSError:
                raise ConfigError(f"OpenD gateway at {creds.host}:{creds.port} is not running")
        return MooMooAdapter(creds)

    _try("Tiger", _tiger)
    _try("Longbridge", _longbridge)
    _try("MooMoo", _moomoo)
    return adapters


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync broker portfolios into Google Sheets.")
    p.add_argument(
        "--seed",
        action="store_true",
        help="First run only: synthesize Opening Balance rows from current positions.",
    )
    p.add_argument(
        "--since",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Only fetch executions on/after this ISO date (default: full history).",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    from config.settings import get_service_account_info, get_spreadsheet_id
    from sheets.writer import PortfolioWriter, SheetClient
    from alerting.notify import notify_safe

    adapters = _build_adapters()
    if not adapters:
        log.error("No brokers configured — nothing to sync. Set broker credentials.")
        return 1

    fx = FxRates()
    client = SheetClient(get_service_account_info(), get_spreadsheet_id())
    writer = PortfolioWriter(client)

    result = run_sync(
        adapters, writer, fx,
        since=args.since, seed=args.seed, notifier=notify_safe,
    )

    log.info(
        "Run %s — stocks +%d/~%d, options +%d/~%d, txns +%d, reconciliation: %s",
        result.status,
        result.stocks_added, result.stocks_updated,
        result.options_added, result.options_updated,
        result.transactions_added, result.reconciliation,
    )
    return 1 if result.status == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())

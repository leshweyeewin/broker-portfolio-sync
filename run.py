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

Seeding (``--seed``): on the very first run for an account whose broker API does
not return full lifetime history, pass ``--seed`` to synthesize Opening Balance
rows from current positions (stable dedup keys, so re-running ``--seed`` upserts
rather than duplicates). See HANDOFF.md for the seeding lifecycle caveat.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from adapters.base import (
    BrokerAdapter,
    CashMovement,
    OptionTrade,
    Position,
    StockTrade,
)
from core.fifo_pl import FifoResult, compute_option_pl, compute_stock_pl
from core.fx import FxRates
from core.reconcile import reconcile, seed_positions
from sheets.writer import (
    DASHBOARD_HEADERS,
    build_option_row,
    build_run_log_row,
    build_stock_row,
    build_transaction_row,
)

log = logging.getLogger("run")

ZERO = Decimal("0")

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
def collect_broker_data(adapter: BrokerAdapter, since: Optional[date]) -> BrokerData:
    """Fetch a single broker's executions/cash/positions.

    Fail-soft: any exception is captured on ``BrokerData.error`` so one broker's
    outage can't sink the others. The run-level status downgrades accordingly.
    """
    try:
        return BrokerData(
            broker=adapter.name,
            stocks=list(adapter.fetch_stock_executions(since)),
            options=list(adapter.fetch_option_executions(since)),
            cash=list(adapter.fetch_cash_movements(since)),
            positions=list(adapter.fetch_positions()),
        )
    except Exception as exc:  # noqa: BLE001 — deliberately broad; recorded, not swallowed
        log.exception("Broker %s fetch failed", adapter.name)
        return BrokerData(broker=adapter.name, error=f"{type(exc).__name__}: {exc}")


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
    realized_sgd_by_broker: dict[str, Decimal],
    reconciliation: str,
) -> list[list[Any]]:
    """A small machine-computed summary block for the Dashboard tab.

    Deliberately minimal (§9 scope): per-broker realized P/L in SGD, open-position
    counts, and run health. The fuller §4 dashboard (net capital in, live
    unrealized valuation) can be layered on later without touching the pipeline.
    """
    brokers = DASHBOARD_HEADERS[1:-1]  # ["Longbridge", "Tiger", "MooMoo"]

    realized_row: list[Any] = ["Realized P/L (SGD)"]
    total = ZERO
    for b in brokers:
        v = realized_sgd_by_broker.get(b, ZERO)
        realized_row.append(float(v))
        total += v
    realized_row.append(float(total))

    counts = Counter(h.broker.value for h in holdings)
    open_row: list[Any] = ["Open Positions"]
    for b in brokers:
        open_row.append(counts.get(b, 0))
    open_row.append(sum(counts.values()))

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        DASHBOARD_HEADERS,
        realized_row,
        open_row,
        ["Status", status, "", "", ""],
        ["Reconciliation", reconciliation, "", "", ""],
        ["Last Run (UTC)", ts, "", "", ""],
    ]


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

    # 1. Fetch every broker (fail-soft per broker).
    datas = [collect_broker_data(a, since) for a in adapters]
    fetch_errors = [(d.broker, d.error) for d in datas if d.error]
    ok = [d for d in datas if d.error is None]

    stocks = [t for d in ok for t in d.stocks]
    options = [t for d in ok for t in d.options]
    cash = [c for d in ok for c in d.cash]
    positions = [p for d in ok for p in d.positions]

    # 2. Seed (first run only) — synthesize Opening Balances from live positions.
    if seed:
        seed_stocks, seed_options = seed_positions(positions, today)
        stocks = seed_stocks + stocks
        options = seed_options + options

    # 3. FIFO realized P/L + remaining holdings.
    stock_result = compute_stock_pl(stocks)
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

    # 6. Reconcile computed holdings against broker-reported positions.
    holdings = list(stock_result.holdings) + list(option_result.holdings)
    recon_warnings = reconcile(holdings, positions)

    # 7. Dashboard summary.
    realized_sgd_by_broker: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in list(stock_result.realizations) + list(option_result.realizations):
        realized_sgd_by_broker[r.broker.value] += realized_sgd.get(r.key, ZERO)

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
        _build_dashboard(status, holdings, dict(realized_sgd_by_broker), reconciliation)
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

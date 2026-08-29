# Architecture & core pipeline

How the daily sync turns three brokers into one Google Sheet, and the guarantees
that make a re-run safe. For the job schedule see
[scheduled-jobs.md](scheduled-jobs.md); for setup see
[getting-started.md](getting-started.md).

## Architecture

All-API pipeline. Each broker SDK is wrapped by an adapter that emits one common
schema; everything downstream is broker-agnostic.

```mermaid
flowchart LR
    subgraph Brokers["Broker APIs (SG accounts)"]
        LB["Longbridge<br/>longport SDK"]
        TG["Tiger<br/>tigeropen SDK"]
        MM["MooMoo<br/>futu-api"]
    end

    OPEND["OpenD gateway<br/>(sidecar container)"]
    MM -. TCP .-> OPEND

    subgraph Job["Scheduled Python job (Cloud Run)"]
        AD["Broker adapters<br/>→ common schema<br/>(normalize §8 in-adapter)"]
        FIFO["FIFO realized P/L<br/>+ unrealized from positions"]
        FX["FX → SGD<br/>(trade-date & current, cached)"]
        WR["idempotent upsert<br/>(_dedup_key)"]
        REC["reconciliation"]
    end

    SM["Google Secret Manager<br/>(tokens, keys, SA JSON)"]
    GS["Google Sheet<br/>(Sheets API, service account)"]
    ALERT["Alert (Telegram/email)"]

    subgraph Weekly["Weekly jobs (read the sheet)"]
        WX["expiry watch"]
        WPL["realized-P/L digest"]
        WL8["lemon8 journal<br/>caption · card · blog"]
        WPE["pancherry export<br/>.ts + Draft PR"]
    end

    LB --> AD
    TG --> AD
    OPEND --> AD
    SM -. secrets .-> Job
    AD --> FIFO --> FX --> WR --> GS
    WR --> REC --> GS
    REC -->|mismatch| ALERT
    Job -->|failure| ALERT

    GS --> Weekly
    WX --> ALERT
    WPL --> ALERT
    WL8 -->|"blog draft · Telegram"| ALERT
    WPE -->|"Draft PR · Telegram"| ALERT

    CSD["Cloud Scheduler (daily 06:00)"] -.triggers.-> Job
    CSW["Cloud Scheduler (weekly, Sun)"] -.triggers.-> Weekly
```

**Runner:** Cloud Run job triggered by Cloud Scheduler. MooMoo's OpenD gateway
runs as a sidecar (Cloud Run multi-container). Bootstrap alternative: GitHub
Actions cron for the Longbridge + Tiger legs (no gateway needed). Locally on
Windows, the daily and weekly jobs run via Task Scheduler (`scripts/*.ps1`).

**Secrets:** Google Secret Manager (or GitHub secrets). Never in code, repo, or
the sheet.

## Data flow per run

```mermaid
flowchart TD
    START([Daily trigger]) --> FETCH["Each adapter fetches<br/>cash / stock / option execs + positions<br/>since last run"]
    FETCH --> NORM["Normalize in-adapter (§8):<br/>tickers, actions, fees,<br/>deposit vs transfer"]
    NORM --> DEDUP["Assign stable _dedup_key (§6)"]
    DEDUP --> FIFO["FIFO engine (§3):<br/>realized P/L on close rows<br/>+ remaining holdings"]
    FIFO --> FXC["FX (§7):<br/>realized & cash @ trade-date rate<br/>unrealized @ current rate"]
    FXC --> WRITE["Sheets writer:<br/>upsert on _dedup_key<br/>write summary blocks"]
    WRITE --> RECON["Reconcile computed qty<br/>vs broker positions (§9)"]
    RECON -->|match| LOG["Run Log row:<br/>status, rows, FX, result"]
    RECON -->|mismatch| ALERT2["Alert + flag in Run Log"]
    FETCH -->|API error / empty / schema drift| FAIL["FAIL LOUD:<br/>write nothing partial, alert"]
    LOG --> DONE([Done])
```

**Correctness guarantees:**
- **Idempotency (§6)** — every row has a stable `_dedup_key`; the writer upserts,
  so re-running any day yields the same sheet state.
- **Reproducibility (§7)** — daily FX rates are cached by `(date, pair)`, so a
  historical row always converts identically.
- **Fail loud (§9)** — on error/empty/schema drift the run stops and alerts; a
  silent half-write is worse than no write.
- **Decimal money** — `Decimal` for all money/qty, explicit currency per row.

## The common schema (adapter ↔ pipeline contract)

Every adapter implements one protocol and returns four normalized dataclasses.
Nothing downstream knows which broker a row came from except via the `Broker`
field.

```mermaid
classDiagram
    class BrokerAdapter {
        <<Protocol>>
        +str name
        +fetch_cash_movements(since) list~CashMovement~
        +fetch_stock_executions(since) list~StockTrade~
        +fetch_option_executions(since) list~OptionTrade~
        +fetch_positions() list~Position~
    }
    class CashMovement {
        date, broker, type
        amount:Decimal, currency
        note, dedup_key
    }
    class StockTrade {
        date, broker, ticker, action
        qty, price, fee, total:Decimal
        currency, dedup_key
    }
    class OptionTrade {
        date, broker, underlying, type
        strike, qty, expiry, action
        premium, fee, multiplier:Decimal
        currency, dedup_key
    }
    class Position {
        broker, asset_type, symbol
        qty, avg_cost:Decimal
        currency, name, market_price
        option_type, strike, expiry
    }
    BrokerAdapter --> CashMovement
    BrokerAdapter --> StockTrade
    BrokerAdapter --> OptionTrade
    BrokerAdapter --> Position

    TigerAdapter ..|> BrokerAdapter
    LongbridgeAdapter ..|> BrokerAdapter : (step 6)
    MooMooAdapter ..|> BrokerAdapter : (step 8)
```

### FIFO P/L engine

Signed FIFO handles both long stock trading and the user's short-premium option
strategies (sell-to-open, buy-to-close). Realized P/L is **native currency, net
of fees**; FX conversion happens afterwards (step 4).

```mermaid
flowchart LR
    EV["Execution<br/>(signed qty)"] --> Q{"Same side as<br/>open position?"}
    Q -->|"Yes / flat"| OPEN["Open a lot<br/>(fees capitalized)"]
    Q -->|"No"| CLOSE["Close lots FIFO<br/>→ Realization<br/>(net of open+close fees)"]
    CLOSE --> FLIP{"Overshoot?"}
    FLIP -->|Yes| OPEN
    FLIP -->|No| HOLD["Remaining lots →<br/>Holding (qty, avg cost)"]
    OPEN --> HOLD
    HOLD --> UNREAL["unrealized_pl(current_price)"]
```

- `compute_stock_pl(trades)` / `compute_option_pl(trades)` → `FifoResult`
  (`realizations`, `holdings`, `realized_by_key`, `total_realized`).
- `Opening Balance` seed rows (§5) become the initial lot; their qty is **signed**
  (positive long, negative short).

## Target workbook (§4)

Machine-owned tabs — written by the pipeline, never hand-edited:

| Tab | Purpose |
|-----|---------|
| **Transactions** | External cash flow only (Deposit/Withdrawal), per broker, native + SGD |
| **Stocks** | One row per buy/sell execution + computed holdings summary (qty, avg cost, market value, unrealized P/L) |
| **Options** | One row per option execution + summary (mirrors the user's existing options tracking) |
| **Dashboard** | Per-broker rollup: deposits, net capital in, account value, fees, unrealized P/L, ROI, plus **rolling realized P/L** for This Week / This Month / This Year — per currency and SGD |
| **Run Log** | One row per run: status, rows added/updated, FX rates used, reconciliation result, warnings/errors |

A hidden `_dedup_key` column on each data tab drives idempotent upserts. The
Stocks/Options tabs also carry one **hand-editable** column, `Reason` — a
free-text trade thesis you fill in, kept as the trailing column so the sync
preserves it on every run (see [content-pipeline.md](content-pipeline.md)).

## Design principles

1. **Decimal money, never float** — coerce via `adapters.base.dec()`.
2. **Explicit, upper-cased currency** on every money row.
3. **Stable `_dedup_key`** (`Broker:fill_id` or hash; `Broker:opening:ticker` for seeds) → idempotent upserts.
4. **Sign convention:** acquisitions negative, sells positive.
5. **Realized P/L is net of fees** (buy fees capitalized, sell fees deducted).
6. **Opening Balance qty is signed** so shorts can be seeded.
7. **Adapters don't compute P/L** — FIFO does; FX converts.
8. **Fail loud** on schema drift / errors — never half-write.

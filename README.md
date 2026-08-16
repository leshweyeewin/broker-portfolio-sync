# broker-portfolio-sync

Automated consolidation of holdings and P/L across three Singapore brokerage
accounts — **Longbridge**, **Tiger Brokers**, **MooMoo** — into a single,
automation-native **Google Sheet**, updated daily and unattended. It replaces a
manual copy-paste workflow into a legacy Excel workbook.

Downstream, the same sheet feeds a **weekly content pipeline**: this repo also
generates the [pancherry](https://pancherry.com) site's trading-journal data
files and the Lemon8/TikTok post drafts (caption + transactions card + blog),
opening them as review-only Draft PRs. The sheet is the single source of truth;
the site is a pure consumer.

> **Design source of truth:** [`BUILD_SPEC.md`](https://claude.ai/public/artifacts/b935403a-b127-4964-ade7-545da2383c71) (external artifact).
> **Continuing the build?** Start with [`HANDOFF.md`](HANDOFF.md) — it records
> what's built, the decisions new code must respect, and step-by-step next actions.

**Non-goals:** no trading/order placement, no investment advice, no migration of
legacy history (fresh start with cost-basis seeding). No auto-publishing — social
posts are uploaded by hand; site PRs are merged by hand.

---

## Build status

**Core sync (daily):**

| # | Step | Module | Status |
|---|------|--------|--------|
| 1 | Common schema + dataclasses | `adapters/base.py` | ✅ done |
| 2 | Tiger adapter | `adapters/tiger.py` | ✅ done |
| 3 | FIFO P/L engine | `core/fifo_pl.py` | ✅ done |
| 4 | FX module (trade-date vs current, cached) | `core/fx.py` | ✅ done |
| 5 | Sheets writer (idempotent upsert) | `sheets/writer.py` | ✅ done |
| 6 | Longbridge adapter | `adapters/longbridge.py` | ✅ done |
| 7 | Seeding + reconciliation | `core/reconcile.py` | ✅ done |
| 8 | MooMoo adapter + OpenD sidecar | `adapters/moomoo.py`, `opend/` | ✅ done |
| 9 | Alerting + Run Log + entrypoint + deploy | `alerting/`, `run.py`, `Dockerfile`, `DEPLOY.md` | ✅ done |
| 10 | Option expiry lifecycle (ITM assignment / OTM worthless) | `core/fifo_pl.py`, `run.py` | ✅ done |
| 11 | Dashboard rolling realized P/L (Week / Month / Year) | `sheets/writer.py` | ✅ done |

**Weekly jobs + content pipeline:**

| # | Step | Module | Status |
|---|------|--------|--------|
| 12 | Expiry watch + realized-P/L digest (Telegram) | `alerting/expiry.py`, `alerting/weekly_pl_alert.py` | ✅ done |
| 13 | Lemon8 weekly journal (caption + card + blog draft) | `lemon8/`, `skills/` | ✅ done |
| 14 | pancherry export → `.ts` data files + auto Draft PR | `pancherry_export/` | ✅ done |
| 15 | Broker ticker-name cache (blog company names) | `core/ticker_names.py` | ✅ done |
| 16 | Per-trade *which/why* (Strategy/Action + manual `Reason`) | `lemon8/`, `sheets/writer.py` | ✅ done |

**232 tests passing.**

---

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

---

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

---

## Scheduled jobs

One daily writer keeps the sheet current; everything else is a **read-only**
weekly job downstream of it. Each has its own entrypoint and PowerShell task
(`scripts/register_*_task.ps1` to install; `scripts/*.ps1` to run).

| Cadence | Job | Entrypoint | Reads / Writes |
|---------|-----|-----------|----------------|
| Daily 06:00 | Portfolio sync | `python run.py` | fetch brokers → **writes** the sheet |
| Weekly (Sun) | Expiry watch | `python -m alerting.expiry` | reads Options → Telegram: contracts expiring ≤ 7 days |
| Weekly (Sun) | Realized-P/L digest | `python -m alerting.weekly_pl_alert` | reads closed trades → Telegram: week's realized P/L by broker |
| Weekly (Sun) | Lemon8 journal | `python -m lemon8.weekly_job` | reads closed trades → caption + card + blog draft, commits blog draft |
| Weekly (Sun) | pancherry export | `python -m core.ticker_names` then `python -m pancherry_export --pr` | refreshes ticker names, regenerates `.ts`, opens/updates a Draft PR |

All weekly jobs are **fail-soft and read-only against the sheet** — a broker or
GitHub being down degrades that leg without touching the sync or the others.

---

## Content pipeline (Sheet → blog + social)

Downstream of the sync, the **Sheet is the single source of truth** for two weekly
deliverables. Generation lives entirely in this repo (it needs the sheet schema,
the percentages-only privacy rule, and the service-account creds); the pancherry
site is a pure **consumer** that just renders the generated data files.

```mermaid
flowchart TD
    GS["Google Sheet<br/>closed trades · P/L · open book"]

    BRK["Broker APIs"]
    TN["core/ticker_names<br/>company-name cache<br/>(ticker_names.json)"]

    subgraph PROD["broker-portfolio-sync (Python) — PRODUCER"]
        PE["pancherry_export<br/>• openPositions.ts (full regen, keeps hidden:)<br/>• weeklyJournals.ts (insert draft, then refresh-in-place)"]
        L8["lemon8/weekly_job<br/>caption + card.png + blog draft<br/>(kind + Reason per trade)"]
    end

    PRB["pancherry-drafts branch<br/>→ Draft PR (auto)"]
    L8B["lemon8-drafts branch<br/>(GitHub API)"]
    UP["Manual upload<br/>Lemon8 / TikTok (no posting API)"]

    subgraph CONS["pancherry repo (TS/React) — CONSUMER"]
        TS["src/data/*.ts"]
        SITE["/trading page"]
    end

    REVIEW{"Human: review PR<br/>polish prose · merge"}
    CF["Cloudflare Pages<br/>pancherry.com/trading"]

    BRK -->|"names (all 3 brokers)"| TN --> PE
    GS --> PE
    GS --> L8
    PE -->|"commit .ts via API"| PRB --> REVIEW
    REVIEW -->|merge| TS --> SITE --> CF
    L8 -->|"blog draft"| L8B
    L8 --> UP
```

**Names are decoupled from the daily sync** — `python -m core.ticker_names`
connects to all three brokers on its own (Tiger/Longbridge direct, MooMoo via
OpenD), fail-soft per broker, and caches `ticker → company name` for the open
-positions grid. The weekly export runs it first, then regenerates the `.ts`.

**Weekly ritual:** run `python -m pancherry_export --pr` → review the Draft PR
(edit prose if the highlights/narrative drifted — the run flags it) → merge.

- **Stat tiles refresh, prose doesn't.** A re-run updates only the numeric
  fields (`trades`/`wins`/`losses`/`winRatePct`/dates) on an existing week's
  entry — your narrative and curated highlights survive. Numbers hard-coded into
  prose sentences are **not** rewritten, so keep prose qualitative.
- **Drift warning.** If more trades close after the draft, the re-run reports the
  new count and whether the auto-picked highlight set changed, so you know to
  revise the story before merging.
- **Nothing goes live unattended** — drafts land on a `*-drafts` branch, never the
  branch Cloudflare Pages builds; publishing is the merge you control.

**Every trade carries its *which* and *why*:**
- **Which / kind** — each trade shows its type, derived from data already synced:
  the option **Strategy** (e.g. `Short Put`, `Cash Secured Put`), or a stock's
  **Buy/Sell**. It appears in the caption top-movers `(kind)`, a blog
  `Strategy / Action` column, and a `STRATEGY` column on the transactions card.
- **Why / thesis** — a manual **`Reason`** column on the Stocks/Options tabs. You
  type the trade thesis by hand in the sheet; it flows into the blog's `Why`
  column and a short note on the caption's top movers. The sheet is the input —
  the daily sync writes `Reason` blank and **preserves whatever you typed** (it
  never clobbers a hand-entered reason), so it's safe to fill in over time. The
  blog's weekly `Rationale & lessons` narrative section stays for the bigger story.

---

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

### FIFO P/L engine (built)

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

---

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
preserves it on every run (see the content pipeline above).

---

## Repository layout

```
broker-portfolio-sync/
├─ adapters/              # broker adapters + common schema
│  ├─ base.py             # ✅ Protocol + dataclasses (the contract)
│  ├─ tiger.py            # ✅ Tiger (tigeropen)
│  ├─ longbridge.py       # ✅ Longbridge (longport)
│  └─ moomoo.py           # ✅ MooMoo (futu-api, via OpenD)
├─ core/                  # pure pipeline logic
│  ├─ fifo_pl.py          # ✅ FIFO realized/unrealized P/L + option expiry
│  ├─ fx.py               # ✅ trade-date vs current, cached (Frankfurter)
│  ├─ reconcile.py        # ✅ seeding + post-write qty check
│  └─ ticker_names.py     # ✅ broker → {ticker: company name} cache (blog)
├─ sheets/writer.py       # ✅ service-account auth, idempotent upsert, Reason-preserve
├─ alerting/              # ✅ Telegram (stdlib urllib, best-effort)
│  ├─ notify.py           #    core send
│  ├─ expiry.py           # ✅ weekly expiry watch (≤ 7 days)
│  └─ weekly_pl_alert.py  # ✅ weekly realized-P/L digest
├─ lemon8/                # ✅ weekly journal (read-only): reader, journal,
│                         #    card (SVG→PNG), blog commit, weekly_job
├─ pancherry_export/      # ✅ .ts generation (exporter) + auto Draft PR (publish)
├─ skills/                # ✅ lemon8-journal-writer skill
├─ scripts/               # ✅ Windows Task Scheduler entrypoints (daily + weekly)
├─ config/settings.py     # secrets from env / Secret Manager
├─ run.py                 # ✅ entrypoint: fetch→[seed]→FIFO→FX→write→reconcile→log→alert
├─ Dockerfile             # ✅ job container (+ .dockerignore)
├─ DEPLOY.md              # ✅ GitHub Actions cron / Cloud Run Job + Scheduler
├─ opend/                 # ✅ MooMoo OpenD sidecar (Dockerfile, compose, entrypoint)
├─ tests/                 # ✅ FIFO, schema, adapters, run, lemon8, export (232)
├─ requirements.txt
├─ BUILD_SPEC.md          # (external link — source of truth)
└─ HANDOFF.md             # continuation spec for the next builder
```

---

## Getting started

### Prerequisites
- Python 3.11, a virtualenv (`.venv/` is used here).
- Broker credentials + a Google Cloud service account — **user-provided** (§13),
  never committed. See the checklist below.

### Install & test
```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pytest -q
```
> **Windows note:** set `PYTHONUTF8=1` before scripts that print SDK objects, or
> cp1252 will crash on non-ASCII output.

### User setup checklist (cannot be done by the pipeline — §13)
- [ ] **Longbridge**: developer verification → token (open.longbridge.com)
- [ ] **Tiger**: RSA keypair + `tiger_id` + account, `license='TBSG'` (developer.itigerup.com)
- [ ] **MooMoo**: Futu/moomoo ID for OpenD login; decide OpenD host (sidecar)
- [ ] **Google Cloud**: service account + JSON key
- [ ] Share the new Sheet with the service-account email (**Editor**)
- [ ] Put all secrets in Secret Manager (or GitHub secrets)
- [ ] Confirm each account's traded currencies (for FX pairs)
- [ ] Choose alert channel (Telegram/email) + provide its credential

---

## Design principles (see [`HANDOFF.md`](HANDOFF.md) §3 for the binding list)

1. **Decimal money, never float** — coerce via `adapters.base.dec()`.
2. **Explicit, upper-cased currency** on every money row.
3. **Stable `_dedup_key`** (`Broker:fill_id` or hash; `Broker:opening:ticker` for seeds) → idempotent upserts.
4. **Sign convention:** acquisitions negative, sells positive.
5. **Realized P/L is net of fees** (buy fees capitalized, sell fees deducted).
6. **Opening Balance qty is signed** so shorts can be seeded.
7. **Adapters don't compute P/L** — FIFO does; FX converts.
8. **Fail loud** on schema drift / errors — never half-write.

---

## License / privacy

Personal project. Real-name public content (blog/social) defaults to **percentages
and reasoning only** — absolute $ amounts are opt-in per post (§11b). Keep all
credentials and the service-account JSON out of the repo (`.gitignore` enforces this).
```

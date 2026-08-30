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

**Non-goals:** no trading/order placement, no investment advice, no migration of
legacy history (fresh start with cost-basis seeding). No auto-publishing — social
posts are uploaded by hand; site PRs are merged by hand.

---

## Documentation

This README is the overview. Feature detail lives in [`docs/`](docs/):

| Doc | What's in it |
|-----|--------------|
| [Architecture & core pipeline](docs/architecture.md) | The all-API sync: adapters → FIFO → FX → sheet, the per-run data flow, correctness guarantees, the common schema, and the target workbook |
| [Scheduled jobs](docs/scheduled-jobs.md) | Every daily/weekly/on-demand entrypoint and what it reads and writes |
| [Analytics & diagnostics](docs/analytics.md) | The `analytics/` risk & diagnostic layer over the sheet (tagger, diagnostics, expiry risk engine, screener) |
| [Options playbook](docs/options-playbook.md) | The read-only options decision-support layer (payoff, trade plans, credit-spread/iron-condor builder) |
| [Content pipeline](docs/content-pipeline.md) | The weekly pancherry export + Lemon8 journal producer→consumer flow |
| [Getting started](docs/getting-started.md) | Prerequisites, install & test, and the user setup checklist |
| [Deployment](DEPLOY.md) | GitHub Actions cron / Cloud Run Job + Scheduler |

## At a glance

All-API pipeline: each broker SDK is wrapped by an adapter that emits one common
schema; everything downstream is broker-agnostic. A daily Cloud Run job fetches
executions and positions, computes **FIFO** realized P/L (signed, net of fees),
converts to SGD with cached daily **FX**, and **idempotently upserts** the Google
Sheet, then **reconciles** computed quantities against broker positions. Money is
`Decimal` throughout, and the run **fails loud** rather than half-writing. See
[docs/architecture.md](docs/architecture.md) for the diagrams.

## What gets notified (Telegram)

All alerts are **read-only decision support** — plans, scores and reminders. Nothing
places, modifies or cancels an order. Three cadences push to Telegram, plus an
on-demand bot you can query any time.

### Daily — `📊 Daily Market & Portfolio Report`

Pushed by the daily job (`run.py --analytics` → `format_telegram_report`). Scanned
over your **full universe** = current holdings + monitored watchlist
(`analytics/data/earnings_dates.json`). Sections (each shown only when it has content):

| Section | What it flags | Based on |
|---|---|---|
| 🚀 **Daily Ticker Movers** | Biggest bullish / bearish movers | Day's % change vs prior close |
| 📅 **Upcoming Earnings (next 2 weeks)** | Names reporting soon, to prep IV-crush spreads | Earnings calendar + ATM-straddle expected move |
| 🔎 **Systematic Short Option Picks** | Short-put (bullish) / short-call (bearish) income | Δ 0.30–0.40, open interest > 500, IV/RV ratio |
| 📈 **Swing Setups** | Technical long entries (see below) | SMA20/50/200 stack, RSI(14), ATR%, 52w-high distance |
| ⏳ **Risk Alerts** | Options expiring within 7 days | Days-to-expiry on open contracts |
| 💸 **Diagnostics** | Fee-drag and IV-crush warnings | Realized-fee ratio + post-earnings IV history |

**Swing setup labels** (the `📈 Swing Setups` block), in priority order:

| Label | Meaning | Trigger |
|---|---|---|
| 🚀 **Breakout** | Riding highs with momentum | Rising MA stack (20>50>200) · within 3% of 52w high · RSI ≥ 60 |
| 🎯 **Pullback-buy** | Classic swing entry zone | Uptrend intact (MA20>MA50) · price dipped to/under MA20 but holds MA50 |
| **Uptrend** | Trending but not at an entry | Above a rising MA stack, above MA20 |
| **Overbought** | Extended — wait, don't chase | RSI > 75 |
| **Downtrend** | Not a long setup | Price < MA20 < MA50 |
| **Base** | Chop / undefined trend | None of the above |

Only **Breakout** and **Pullback-buy** are surfaced as actionable long entries; they
sort to the top.

### Weekly — `🗓️ Weekly Options Digest`

Pushed by `python -m alerting.weekly_digest` on a weekly schedule. Two scans, one message:

- **Earnings credit spreads** — runs the IV-crush screener over the *same* earnings
  universe as the daily report, grading each name (FOCUS / WATCH / SKIP) and, where the
  edge is RICH/FAIR, attaching live put-credit / call-credit / iron-condor strikes.
- **Wheel from current positions** — reads your holdings from the Sheet and scans
  cash-secured puts, covered calls and PMCC diagonals against what you actually hold
  (no ticker needed). Falls back to yfinance quotes when the broker feed is down.

Other weekly pushes already in place: `alerting/weekly_pl_alert.py` (realized-P/L digest)
and `alerting/expiry.py` (options expiring ≤ 7 days).

### On demand — the Telegram bot (`python -m alerting.bot`)

Long-polls Telegram and answers instantly:

| Command | Needs a ticker? | Returns |
|---|---|---|
| `/quote NVDA` (or bare `NVDA`) | yes | Price, trend/RSI/ATR, 52w position, next earnings + expected move |
| `/directional NVDA` | yes | Bull-call / long / CSP / covered-call / PMCC board for one name |
| `/midweek SPY` | yes | Short-dated (0–5 DTE) expiry templates |
| `/spreads` | **no** | Earnings credit-spread scan over your watchlist |
| `/wheel` | **no** | CSP / covered-call / PMCC from your current positions |

`/spreads` and `/wheel` take no argument — they scan your watchlist and your positions,
the same inputs as the weekly digest.

## Key Tools (Playbook & Analytics)

The repository contains several offline decision-support tools you can run locally:

| Tool | Run Command | Description |
|---|---|---|
| **IV Crush Screener** | `python -m analytics.earnings.iv_crush` | Screens for earnings plays with a RICH/FAIR edge, grades them, and pipes them into credit-spread builders using live quotes. |
| **Earnings Planner** | `python -m analytics.earnings.earnings_planner` | Runs the IV Crush screener over your watchlist and auto-fills the "Earnings Plan" tab in your Google Sheet. |
| **IV Logger** | `python -m analytics.earnings.iv_logger` | Logs daily ATM-IV snapshots to `data/iv_history.json` to build the required historical context for the screener. |
| **Income Workspace** | `python -m analytics.options.income_workspace` | Auto-detects eligible shares and cash to plan Wheel, Covered Call, Cash-Secured Put, and PMCC trades. |
| **Directional Builder** | `python -m analytics.options.directional_builder` | Plans and scores debit spreads and long options. |
| **Mid-Week Planner** | `python -m analytics.options.mid_week_planner` | Supports short-dated (Mon/Wed) setups and weeklies. |
| **Strategy Journal** | `python -m analytics.options.journal` | Correlates original trade plans against final realized P/L to grade your execution discipline. |
| **Portfolio Risk** | `python -m analytics.options.portfolio_risk` | Aggregates open max-loss and enforces configurable risk guardrails across linked accounts. |

## Quickstart

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pytest -q
```
> **Windows note:** set `PYTHONUTF8=1` before scripts that print SDK objects, or
> cp1252 will crash on non-ASCII output.

Full setup (broker credentials, service account, secrets) is in
[docs/getting-started.md](docs/getting-started.md).

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
├─ analytics/             # ✅ portfolio analytics, diagnostics, options playbook
│  ├─ reporting/          #    Analytics orchestrator & Telegram report formatter
│  ├─ data/               #    Local JSON caches (earnings dates, IV history)
│  ├─ earnings/           #    Earnings & IV-crush domain (see docs/analytics.md)
│  ├─ screening/          #    Signal scanners (screener, market_scan, swing, tagger)
│  ├─ risk/               #    Risk & sizing (risk_engine, position_sizing, diagnostics)
│  └─ options/            #    Options-playbook domain (see docs/options-playbook.md)
├─ sheets/writer.py       # ✅ service-account auth, idempotent upsert, Tag & Reason
├─ alerting/              # ✅ Telegram (stdlib urllib, best-effort)
│  ├─ notify.py           #    core send
│  ├─ bot.py              # ✅ on-demand bot (/quote /directional /midweek /spreads /wheel)
│  ├─ weekly_digest.py    # ✅ weekly options digest (earnings spreads + wheel)
│  ├─ expiry.py           # ✅ weekly expiry watch (≤ 7 days)
│  ├─ weekly_pl_alert.py  # ✅ weekly realized-P/L digest
│  ├─ take_profit.py      # ✅ live long-option +50% take-profit alert
│  └─ earnings_iv_exit.py # ✅ live post-earnings IV-exit reminder
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
├─ tests/                 # ✅ test suite (480+ passing tests)
└─ requirements.txt
```

---

## License / privacy

Personal project. Real-name public content (blog/social) defaults to **percentages
and reasoning only** — absolute $ amounts are opt-in per post (§11b). Keep all
credentials and the service-account JSON out of the repo (`.gitignore` enforces this).

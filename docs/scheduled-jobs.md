# Scheduled jobs

One daily writer keeps the sheet current; everything else is a **read-only**
weekly or on-demand job downstream of it. Each has its own entrypoint and
PowerShell task. The `scripts/` folder is grouped by role: runners live under
`scripts/daily/` and `scripts/weekly/` (the job that does the work), and their
one-time installers live under `scripts/setup/` (each registers the Windows
scheduled task that invokes its runner). For example, `scripts/daily/daily_sync.ps1`
is installed by `scripts/setup/daily_sync_register.ps1`.

| Cadence | Job | Entrypoint | Reads / Writes |
|---------|-----|-----------|----------------|
| Daily 06:00 | Portfolio sync + Market Analytics | `python run.py --analytics` | fetch brokers → **writes** sheet + strategy tags, daily movers, earnings alerts & short option picks → Telegram |
| Weekly (Sun) | Expiry watch | `python -m alerting.expiry` | reads Options → Telegram: contracts expiring ≤ 7 days |
| Weekly (Sun) | Realized-P/L digest | `python -m alerting.weekly_pl_alert` | reads closed trades → Telegram: week's realized P/L by broker |
| Weekly (Sun) | Options digest | `python -m alerting.weekly_digest` | reads watchlist + holdings → Telegram: earnings credit-spread scan + wheel (CSP/CC/PMCC) from current positions |
| Weekly (Sun) | Lemon8 journal | `python -m lemon8.weekly_job` | reads closed trades → caption + card + blog draft, commits blog draft |
| Weekly (Sun) | pancherry export | `python -m core.ticker_names` then `python -m pancherry_export --pr` | refreshes ticker names, regenerates `.ts`, opens/updates a Draft PR |
| On-demand | Standalone Market Analytics | `python -m analytics.report --notify` | runs strategy tagging, diagnostics, movers, earnings & option screener |
| On-demand | Telegram quote/options bot | `python -m alerting.bot` | long-polls Telegram → answers `/quote`, `/directional`, `/midweek` (per-ticker) and `/spreads`, `/wheel` (watchlist / positions) |
| Intraday | Long-option take-profit | `python -m alerting.take_profit` | reads **live** broker positions → Telegram: long options at/above +50% unrealized (`--dry-run` prints instead) |
| Intraday | Earnings IV Exit | `python -m alerting.earnings_iv_exit` | reads **live** broker positions → Telegram: alerts to close short premium options day before earnings |
| Daily 16:30 | IV Logger | `python -m analytics.earnings.iv_logger` | fetches options chain → **writes** `analytics/data/iv_history.json` |
| On-demand | Earnings Planner | `python -m analytics.earnings.earnings_planner` | reads `iv_history.json` → **writes** Earnings Plan sheet tab |
| On-demand | Position sizing (2% rule) | `python -m analytics.risk.position_sizing --equity 25000 --entry 180 --stop 174` | prints shares/contracts sized to a fixed % of equity (options: `--max-loss-per-contract`) |

All weekly jobs are **fail-soft and read-only against the sheet** — a broker or
GitHub being down degrades that leg without touching the sync or the others.

See also: [analytics.md](analytics.md) for the diagnostic layer these jobs drive,
and [options-playbook.md](options-playbook.md) for the read-only options tooling.

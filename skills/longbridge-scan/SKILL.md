---
name: longbridge-scan
description: Agent-run daily trading scan over the Longbridge MCP — swing setups, options-income candidates, a fresh-idea screen, and position sizing — for the tickers on your live Longbridge watchlist. Writes one dated brief into analytics/data/daily_brief/.
---

# Longbridge Daily Scan

This is an **agent skill**: it runs inside a Claude chat that has the Longbridge
MCP connected (server `bc7887a1-…`, tools prefixed `mcp__…__quote`,
`option_quote`, `consensus`, etc.). It is **not** part of `run.py` / the cron
pipeline — the MCP only exists while an agent is driving. The unattended
`analytics/` scan stays on yfinance + the `longport` SDK; this skill is the
richer, on-demand layer that reads data yfinance can't give cleanly:
**analyst price targets & ratings, EPS/revenue estimates, and capital flow**.

**Account entitlements (verified 2026-10-05, re-check if they change):**
- US + HK **quotes and candlesticks** — yes (MCP *and* the longport SDK). Prefer
  the SDK for the swing board's candles (local, no per-call MCP cost); use the
  MCP for everything research-y below.
- **US options IV / Greeks / open-interest — NO.** `option_quote` returns
  `301604 "no quote access"`; the SDK entitlement table shows
  `USOption: no access`. Strikes/expiries list, but Greeks do not. Section 2
  below is therefore gated on buying the US options (OPRA) data package.
- Price targets live in **`institution_rating`** (target mean/high/low + buy/hold/
  sell counts). The **`consensus`** tool returns EPS/revenue *estimates*, not a $
  price target — don't confuse them.

## The contract (load-bearing — from the Longbridge workshop)

1. **Always call tools.** Every price, target, IV and date comes from a
   Longbridge tool, never from memory. Start the work with "Using Longbridge".
2. **Stamp every number with the time you read it** (SGT). A number without a
   time is a guess.
3. **If a figure is unavailable, write `n/a` — never estimate it.** (Exception:
   values this skill explicitly derives, e.g. position-sizing arithmetic.)
4. **Say the source.** End the brief by listing which Longbridge tools were
   called, and name any figure that did not come from a tool.
5. **Read-only.** This skill never places/changes orders and never creates a
   watchlist or alert without showing the user first and getting a yes.

## Tickers — from the live watchlist

Read the user's Longbridge watchlist with `watchlist` (fall back to
`sharelist_list` → `sharelist_detail`). Use those symbols as the universe for
sections 1–2. Do not hard-code a list here — the watchlist is the single source.
Group names by the themes in `analytics/screening/swing.py` (`THEMES`:
Storage/Memory, AI/Compute, Neo Cloud, Optical, MAG7, AI Power, etc.);
anything unlisted falls into "Other".

## What to produce — one brief, four sections

Write to `analytics/data/daily_brief/YYYY-MM-DD.md` (create the dir if needed),
newest run overwrites same-day. Keep it tight; this is a read, not an essay.

### 1. Swing board (directional layer)
For each watchlist name, from `quote` + `candlesticks` (daily, ~1y):
- last price, today's %move, and the trend stack (price vs 20/50/200-day).
- RSI, ATR%, position vs 20-day mean and 52-week high.
- a plain-English label, reusing `swing.py`'s set:
  **Breakout / Pullback-buy / Uptrend / Base / Overbought / Downtrend / No-data**.
- next earnings date (`finance_calendar`) and ex-dividend date (`corp_action` /
  `dividend_detail`) if within ~30 days.
Group by theme. The ATR risk bracket is the same policy as `swing.py`:
**stop 1.5× ATR below entry, target 3× ATR above (2:1 reward:risk)** — don't
redefine it, cite it.

### 2. Options-income candidates (short put / covered call)
**Gated on a US options data subscription this account does not currently have**
(see entitlements above). Check once per run with a single `option_quote` on any
liquid contract: if it returns `301604`, write this section as:
> _Options IV/Greeks unavailable — account has no US options (OPRA) subscription.
> Ran the yfinance estimated-delta screen instead (`analytics/screening/screener.py`)._
and fall back to that Python screener for the income picks. Do **not** fabricate
Greeks. When the subscription IS added, use the real chain
(`option_chain_expiry_date_list` → `option_chain_info_by_date` for strikes/symbols
→ `option_quote` for IV/Greeks/OI) with the `screener.py` filters — one source of
truth: **Delta 0.30–0.40**, **OI > 500**, **spread ≤ $0.10**, ~30–45 DTE. IVP
needs IV history the MCP doesn't serve → write `n/a`, never pass IV off as a
percentile.

### 3. Fresh-idea screen (outside the watchlist)
Run `screener_search` / `screener_indicators` for the workshop screen, adapted
to the user's swing style — e.g. **market cap > US$10B, PE < 25, MACD golden
cross in the last 5 trading days**, US + HK, sorted by market cap. Return the
top ~10 with one line each on why it screened and the time of the screen. Flag
any that overlap an existing theme bucket.

### 4. Position sizing (only when a candidate is named)
If the user points at a specific name, run the workshop sizer on **real** data:
- `quote` for current price, `institution_rating` for the analyst target
  (`.analyst.target` mean / `.instratings.target`) + buy/hold/sell counts.
- Inputs: entry = current price, stop = user's % (default swing bracket:
  1.5× ATR), max risk = user's $ (ask if unset).
- Work out, arithmetic line by line: share count & cost, exact stop price,
  upside to target (% and $), and reward:risk ratio.
- Stamp the read-time. **Do not place anything.**

## Finish
- One closing sentence per the "so what?": what deserves attention today.
- The **tool-audit footer**: list every Longbridge tool called with each call's
  timestamp, then note any figure that did not come from a tool.

## Delivery (Telegram)
After writing the dated brief, push a condensed (~10-line) version to Telegram
with the repo's existing sender — no new dependency:
`from alerting.notify import notify_safe; notify_safe(text)` (config via
`config.settings`, auto-splits at 4000 chars). Keep the full brief in the file;
Telegram gets the phone-sized summary + the file path. Note: the repo's `.env` is
UTF-16 — load it by stripping null bytes (`open('.env','rb').read().replace(b'\x00',b'')`)
before reading the Longbridge/Telegram vars.

## Optional writes (ask first)
Only on explicit request, and only after showing the user exactly what will be
created: add a name to a watchlist group (`create_watchlist_group` /
`update_watchlist_group`) or set a price alert (`alert_add`). These change the
account and need the watchlist scope.

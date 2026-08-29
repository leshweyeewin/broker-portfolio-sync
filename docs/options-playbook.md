# Options playbook (read-only decision-support)

`analytics/options/` is a **read-only decision-support layer** built from the
Moomoo options-playbook material. It plans, scores and journals option
strategies; it never fetches a chain into strategy logic, submits an order, or
gives personalised advice. Its full backlog and delivery slices live in
[`moomoo_option_playbook/FEATURES_TO_BUILD.md`](../moomoo_option_playbook/FEATURES_TO_BUILD.md).

```
analytics/options/
├─ option_chain.py     # Normalised quotes, quality gates + snapshot store
├─ payoff.py           # Pure Decimal expiry payoff / max-risk calculator
├─ market_context.py   # Technical, earnings and expected-move context card
├─ trade_plans.py      # Local read-only plans + lifecycle validation
├─ income_workspace.py # Wheel, CC, CSP and PMCC research workspace
└─ strategies.py       # Pure credit-spread / iron-condor builder + scoring
```

## Foundations (Slices 1–2)

The payoff/chain/plan foundation is local and read-only: it never sends broker
orders. `analytics.options.payoff` provides a pure `Decimal` API for expiry
payoff/risk (unbounded results are `None`, never a made-up large number).
`analytics.options.option_chain` normalises quotes, gates them on liquidity, and
persists a snapshot so a plan can reproduce the decision that informed it.

Use the trade-plan CLI to save a plan with its evidence snapshot reference:

```bash
./.venv/Scripts/python.exe -m analytics.options.trade_plans create --ticker AAPL --strategy "Bull Call" --expiry 2026-09-18 --leg buy:call:200:5 --leg sell:call:210:2 --entry-trigger breakout --invalidation "close below 195" --exit-rule "take 50%" --risk-budget 400 --snapshot-id <snapshot-id> --approve
./.venv/Scripts/python.exe -m analytics.options.trade_plans list
```

`--approve` rejects incomplete, mixed-expiry, over-budget, or unbounded-risk
plans. The default local stores (`analytics/trade_plans.json` and
`analytics/option_snapshots.json`) are user-owned evidence, not broker data.

## Credit-spread / iron-condor builder (Slice 3)

`analytics.options.strategies` is a **pure library** — it consumes an
`OptionChainSnapshot` (from `option_chain`) and returns scored, defined-risk
candidates; it never fetches a chain, sends a notification, or places an order.

- `build_put_credit_spreads` / `build_call_credit_spreads` / `build_iron_condors`
  each return a `SpreadScan` whose `candidates` are ranked by an **explainable
  score** (return-on-risk, liquidity, expected-move buffer, event risk, optional
  IV rank) and whose `rejections` list **every discarded pair with a reason**, so
  a report can explain why nothing qualified instead of showing a blank list.
- Gating is configurable via `SpreadFilters` (documented defaults: 21–45 DTE,
  short-leg delta 0.30–0.40, ≥100 open interest, ≤15% bid-ask, expected-move
  buffer, `block_earnings`).
- RSI is surfaced only as **signal context**, never a buy/sell instruction.
- IV rank must be supplied by the caller — the builder **never fabricates** one
  from current IV.
- A live-provider → snapshot adapter is intentionally **not** wired here
  (offline-first; integrate a provider only behind the same model).

## Safety boundary

- No order placement — plans, calculators and alerts stay read-only against
  brokers.
- `Decimal` for all cash/price/quantity/strike math; missing market data is never
  turned into zero; unbounded risk is represented explicitly.
- Broker fills and positions remain the accounting source of truth; a trade plan
  is a separate, user-authored intent record that may be linked to fills later.

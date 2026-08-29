# Analytics & diagnostic module (`analytics/`)

A diagnostic and risk-management layer that processes the Google Sheet data
maintained by the sync. It is read-only against the sheet and never places
orders. For the options decision-support layer (payoff, trade plans, spread
builder) see [options-playbook.md](options-playbook.md).

```mermaid
flowchart TD
    GS["Google Sheet (Stocks & Options)"] --> READ["PortfolioWriter.read_all_*_trades()"]
    READ --> TAG["Strategy Tagger (tagger.py)<br/>• Earnings IV Crush (±1d of earnings)<br/>• Day Trades (same-day open & close)<br/>• Medium-Term (held ≥2 days)"]
    TAG --> DIAG["Diagnostic Calculators (diagnostics.py)<br/>1. IV Crush: win/loss asymmetry (loss > 2× win)<br/>2. Fee Drag: day trade fees > 15% of gross profit<br/>3. Medium-Term: total realized P/L"]
    TAG --> RISK["Expiry Risk Engine (risk_engine.py)<br/>Open options expiring in 1–14 days:<br/>• CLOSE POSITION (earnings IV capture)<br/>• ROLL SPREAD (overnight gap adjustment)<br/>• CUT TRADE (thesis invalidated)<br/>• ROLL TIMELINE (macro intact, roll monthly)<br/>• NEEDS REVIEW (technical check)"]
    CHAIN["Tiger QuoteClient<br/>(Live option chains)"] --> SCR["Option Screener (screener.py)<br/>• IVP ≥ 70%<br/>• Delta 0.30–0.40<br/>• OI > 500<br/>• Spread ≤ $0.10"]
    DIAG --> REP["Report & Alerts (report.py)"]
    RISK --> REP
    SCR --> REP
    REP --> TG["Telegram Alert / CLI Output"]
```

## Package layout

```
analytics/
├─ reporting/          # Report orchestration
│  └─ report.py          # Analytics orchestrator & Telegram report formatter
├─ data/               # Local JSON caches (earnings dates, IV history)
├─ earnings/           # Earnings & IV-crush domain
│  ├─ earnings.py         # Earnings date lookup (yfinance API + JSON cache)
│  ├─ earnings_move.py    # Historical earnings-move study (Step-4 edge)
│  ├─ earnings_planner.py # Writes the "Earnings Plan" sheet tab
│  ├─ iv_logger.py        # Daily ATM-IV snapshot → data/iv_history.json
│  ├─ iv_crush.py         # IV-crush screener (4-step playbook grade)
│  └─ iv_crush_history.py # Crush consistency + magnitude (Steps 2 & 3)
├─ screening/          # Signal scanners
│  ├─ screener.py         # Live option screener via Tiger QuoteClient
│  ├─ market_scan.py      # Daily movers, earnings calendar, short-option picks
│  ├─ swing.py            # Swing-setup scanner (Breakout / Pullback-buy / etc.)
│  └─ tagger.py           # Strategy tagging (IV Crush, Day Trade, Medium-Term)
├─ risk/               # Risk & sizing
│  ├─ risk_engine.py      # 1–14 DTE expiry risk engine + playbook signals
│  ├─ position_sizing.py  # Fixed-fractional 2% position sizing
│  └─ diagnostics.py      # 3 diagnostic calculators (IV crush, fee drag, alpha)
└─ options/            # Options-playbook domain (see options-playbook.md)
```

## How to check & run analysis

You can run the analytics anytime in your terminal:

```bash
# 1. Run analysis and print report to terminal (read-only against your sheet)
./.venv/Scripts/python.exe -m analytics.reporting.report

# 2. Run analysis and send Telegram alert
./.venv/Scripts/python.exe -m analytics.reporting.report --notify

# 3. Run full sync and immediately output analytics
./.venv/Scripts/python.exe run.py --analytics
```


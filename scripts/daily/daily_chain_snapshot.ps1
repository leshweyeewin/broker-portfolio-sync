$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo
$env:PYTHONUTF8=1
# Records one option-chain snapshot per watchlist ticker into
# analytics/data/chain_history/<TICKER>/<date>.json.gz for stage-3 backtests.
# Run after the US close (04:30+ SGT) so the chain reflects that session's close.
& ".\.venv\Scripts\python.exe" -m analytics.backtest.snapshots record --from-brokers

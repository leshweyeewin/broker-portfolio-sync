$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo
$env:PYTHONUTF8=1
# Watchlist derived from live MooMoo + Tiger holdings (falls back to a built-in
# default list if the brokers are unreachable).
& ".\.venv\Scripts\python.exe" -m analytics.earnings.iv_logger --from-brokers

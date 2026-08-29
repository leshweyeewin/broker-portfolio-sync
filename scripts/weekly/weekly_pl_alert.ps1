# Weekly realized-P/L Telegram digest (Windows Task Scheduler entrypoint).
#
# Reads the sheet's closed trades (read-only) and Telegrams the realized P/L for
# the week just ended, broken down by broker + total. Realized only — booked P/L
# on closed trades, not the mark-to-market swing on open positions.
#
# Runs Sunday evening, leading off the weekly digests (before the expiry alert).
# Register with scripts/setup/weekly_pl_alert_register.ps1. Logs to logs/.

# Same PS 5.1 gotcha as daily_sync.ps1 — do NOT set ErrorActionPreference 'Stop'.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("weeklypl_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" -m alerting.weekly_pl_alert *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop weekly-P/L logs older than 30 days.
Get-ChildItem $logDir -Filter "weeklypl_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exit

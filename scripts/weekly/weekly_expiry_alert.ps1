# Weekly options-expiry Telegram alert (Windows Task Scheduler entrypoint).
#
# Reads the Options tab (read-only), nets each contract's open quantity, and
# Telegrams every contract expiring within the next 7 days. Runs Sunday evening,
# separate from the daily 06:00 sync (scripts/daily_sync.ps1) — it does NOT sync
# brokers, it just reads whatever the last sync wrote and sends the heads-up.
#
# Register with scripts/setup/weekly_expiry_alert_register.ps1. Each run logs to logs/.

# NOTE: same PS 5.1 gotcha as daily_sync.ps1 — do NOT set ErrorActionPreference
# to 'Stop', or a native stderr line from a dependency aborts the run. Continue
# lets stderr flow to the log; the Python exit code signals success/failure.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("expiry_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" -m alerting.expiry *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop expiry logs older than 30 days.
Get-ChildItem $logDir -Filter "expiry_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Exit code: 0 = alert delivered, non-zero = delivery failed (visible in Task
# Scheduler's Last Run Result, so a broken TELEGRAM_* config surfaces).
exit $exit

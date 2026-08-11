# Daily broker portfolio sync (Windows Task Scheduler entrypoint).
#
# Incremental forward run: `run.py --since <7 days ago>` (NO --seed). run.py
# loads the persisted Opening Balances from the sheet into FIFO and drops any
# fetched fill dated on/before the seed, so holdings reconcile and realized P/L
# is correct while new trades accumulate. The 7-day window catches late-settling
# fills; the idempotent upsert dedups the overlap.
#
# One-time bootstrap (already done) was `run.py --seed --since <future>`.
#
# MooMoo only syncs if the OpenD gateway is running at run time; if it isn't,
# MooMoo fails-soft (PARTIAL) and Tiger + Longbridge still sync.
#
# Register with scripts/register_daily_task.ps1. Each run logs to logs/ (gitignored).

# NOTE: do NOT set $ErrorActionPreference='Stop'. The broker SDKs log to stderr,
# and under Task Scheduler's Windows PowerShell 5.1, Stop turns any native stderr
# line into a fatal NativeCommandError that aborts the run. Continue lets stderr
# flow to the log; run.py's own exit code is what signals success/failure.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$since = (Get-Date).AddDays(-7).ToString("yyyy-MM-dd")
$log = Join-Path $logDir ("sync_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" run.py --since $since *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop runs older than 30 days.
Get-ChildItem $logDir -Filter "sync_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Surface run.py's real exit code as the task result (0 = success).
exit $exit

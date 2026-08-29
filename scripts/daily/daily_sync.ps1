# Daily broker portfolio sync (Windows Task Scheduler entrypoint).
#
# Forward run: `run.py` with NO --since and NO --seed. run.py loads the persisted
# Opening Balances (seeded as of 2026-08-09) from the sheet into FIFO and drops
# every fetched fill dated on/before that seed, keeping the whole forward journal.
#
# Why no --since: the sheet persists only the Opening-Balance rows, not the
# journaled Buy/Sell rows, so FIFO must re-see *all* post-seed fills each run to
# price a later sale of a post-seed lot correctly. A rolling window would lose the
# basis of anything bought before it. The idempotent upsert means re-fetching the
# forward journal every run only updates in place — nothing duplicates or drifts.
# (Tiger's API returns ~1 year regardless of --since; the pre-seed part is dropped.)
#
# One-time bootstrap (already done) was `run.py --seed --since 2026-08-10`.
#
# MooMoo only syncs if the OpenD gateway is running at run time; if it isn't,
# MooMoo fails-soft (PARTIAL) and Tiger + Longbridge still sync.
#
# Register with scripts/daily_sync_register.ps1. Each run logs to logs/ (gitignored).

# NOTE: do NOT set $ErrorActionPreference='Stop'. The broker SDKs log to stderr,
# and under Task Scheduler's Windows PowerShell 5.1, Stop turns any native stderr
# line into a fatal NativeCommandError that aborts the run. Continue lets stderr
# flow to the log; run.py's own exit code is what signals success/failure.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("sync_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" run.py --analytics *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop runs older than 30 days.
Get-ChildItem $logDir -Filter "sync_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# Surface run.py's real exit code as the task result (0 = success).
exit $exit

# Weekly pancherry data-file refresh (Windows Task Scheduler entrypoint).
#
# Reads the portfolio sheet (read-only) and writes into the pancherry site clone:
#   * src/data/openPositions.ts  — fully regenerated from the current book
#   * src/data/weeklyJournals.ts — this week's entry appended as published:false
# Then Telegrams a "draft ready — review & push" heads-up.
#
# Runs Sunday evening, after the Lemon8 job. It does NOT commit or push —
# publishing the public site stays a manual step (review the diff, edit the
# narrative, flip published:true, git push).
#
# Register with scripts/register_weekly_pancherry_task.ps1. Logs to logs/.

# Same PS 5.1 gotcha as daily_sync.ps1 — do NOT set ErrorActionPreference 'Stop'.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("pancherry_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" -m pancherry_export *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop pancherry logs older than 30 days.
Get-ChildItem $logDir -Filter "pancherry_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exit

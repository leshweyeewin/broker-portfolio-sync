# Weekly pancherry data-file refresh (Windows Task Scheduler entrypoint).
#
# Reads the portfolio sheet (read-only) and writes into the pancherry site clone:
#   * src/data/openPositions.ts  — fully regenerated from the current book
#   * src/data/weeklyJournals.ts — this week's entry appended as published:false
# Then commits those files to the pancherry-drafts branch and opens/updates a
# Draft PR (--pr), and Telegrams the PR link.
#
# Runs Sunday evening, after the Lemon8 job. The PR is a DRAFT against a drafts
# branch — never the branch Cloudflare Pages builds — so nothing goes live
# unattended. Publishing stays a manual step: review the PR, polish the
# narrative, flip published:true, merge.
#
# Requires GITHUB_TOKEN (repo scope) + PANCHERRY_REPO_SLUG in .env.
# Register with scripts/weekly_pancherry_export_register.ps1. Logs to logs/.

# Same PS 5.1 gotcha as daily_sync.ps1 — do NOT set ErrorActionPreference 'Stop'.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("pancherry_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

# Refresh company names from the brokers first (Tiger/Longbridge direct; MooMoo
# needs OpenD up). Best-effort — a broker that's down is skipped, cached names
# kept — and it never blocks the export.
& "$repo\.venv\Scripts\python.exe" -m core.ticker_names *>> $log

& "$repo\.venv\Scripts\python.exe" -m pancherry_export --pr *>> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop pancherry logs older than 30 days.
Get-ChildItem $logDir -Filter "pancherry_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exit

# Weekly Lemon8 trading-journal job (Windows Task Scheduler entrypoint).
#
# Reads the sheet's Closed positions (read-only), generates a copy-paste journal
# package (caption + blog-draft markdown + SVG/PNG card) per trade closed in the
# last 7 days into lemon8_out\<week>\, optionally commits blog drafts to GitHub
# (skipped unless GITHUB_TOKEN / BLOG_REPO are set), and Telegrams a heads-up.
#
# Runs Sunday evening, after the expiry alert. It does NOT sync brokers or post
# to Lemon8/TikTok (no posting API) — you upload the generated files manually.
#
# Register with scripts/setup/weekly_lemon8_register.ps1. Logs to logs/.

# Same PS 5.1 gotcha as daily_sync.ps1 — do NOT set ErrorActionPreference 'Stop'.
$ErrorActionPreference = "Continue"
$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("lemon8_" + (Get-Date).ToString("yyyyMMdd_HHmmss") + ".log")

& "$repo\.venv\Scripts\python.exe" -m lemon8.weekly_job *> $log
$exit = $LASTEXITCODE

# Keep the log directory tidy — drop lemon8 logs older than 30 days.
Get-ChildItem $logDir -Filter "lemon8_*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exit

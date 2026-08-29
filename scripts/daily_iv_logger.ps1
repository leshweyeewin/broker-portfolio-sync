$repo = "D:\Learn\Google\broker-portfolio-sync"
Set-Location $repo
$env:PYTHONUTF8=1
& ".\.venv\Scripts\python.exe" -m analytics.iv_logger NVDA CRM CRWD COST

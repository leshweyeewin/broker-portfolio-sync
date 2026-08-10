# Registers the daily portfolio-sync task in Windows Task Scheduler.
# Re-run with a different -At to change the time, e.g.:
#   .\scripts\register_daily_task.ps1 -At "18:30"
param([string]$At = "06:00")

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\daily_sync.ps1`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# Runs only while the user is logged on (no stored password needed, and OpenD —
# an interactive app — is only up when logged on anyway). StartWhenAvailable
# fires a missed run when the PC next wakes.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerPortfolioSync" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily broker portfolio sync (Tiger, Longbridge, MooMoo). MooMoo requires the OpenD gateway running at run time; otherwise it fails-soft and the other two still sync." `
    -Force

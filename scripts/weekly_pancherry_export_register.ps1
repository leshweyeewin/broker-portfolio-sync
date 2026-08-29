# Registers the weekly pancherry data-refresh job in Windows Task Scheduler.
# Default: Sunday 18:45 (after the 18:30 Lemon8 job). Change with -At / -Day:
#   .\scripts\register_weekly_pancherry_task.ps1 -At "19:15"
param(
    [string]$At = "18:45",
    [ValidateSet("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")]
    [string]$Day = "Sunday"
)

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\weekly_pancherry_export.ps1`""

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At

# Runs only while logged on. StartWhenAvailable fires a missed run on next wake.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerPancherryExport" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly pancherry site data refresh. Regenerates openPositions.ts and appends this week's weeklyJournals.ts draft (published:false) from the portfolio sheet, then Telegrams a review-and-push heads-up. Does not commit or push." `
    -Force

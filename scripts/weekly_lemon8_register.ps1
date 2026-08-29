# Registers the weekly Lemon8 journal job in Windows Task Scheduler.
# Default: Sunday 18:30 (after the 18:00 expiry alert). Change with -At / -Day:
#   .\scripts\register_weekly_lemon8_task.ps1 -At "19:00"
param(
    [string]$At = "18:30",
    [ValidateSet("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")]
    [string]$Day = "Sunday"
)

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\weekly_lemon8.ps1`""

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At

# Runs only while logged on. StartWhenAvailable fires a missed run on next wake.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerLemon8Journal" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly Lemon8 trading-journal package generator. Reads the portfolio sheet, writes copy-paste posts + PNG cards to lemon8_out\, optionally commits blog drafts to GitHub (draft branch), and Telegrams a heads-up." `
    -Force

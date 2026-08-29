# Registers the weekly realized-P/L Telegram digest in Windows Task Scheduler.
# Default: Sunday 17:45 (leads off the weekly digests, before the 18:00 expiry
# alert). Change with -At / -Day:
#   .\scripts\setup\weekly_pl_alert_register.ps1 -At "18:00"
param(
    [string]$At = "17:45",
    [ValidateSet("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")]
    [string]$Day = "Sunday"
)

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\weekly\weekly_pl_alert.ps1`""

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At

# Runs only while logged on. StartWhenAvailable fires a missed run on next wake.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerWeeklyPLAlert" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly realized-P/L Telegram digest. Reads the portfolio sheet and messages the week's realized P/L (SGD) per broker + total. Realized/closed trades only." `
    -Force

# Registers the weekly options-expiry alert in Windows Task Scheduler.
# Default: Sunday 18:00. Re-run with a different -At / -Day to change it, e.g.:
#   .\scripts\weekly_expiry_alert_register.ps1 -At "17:30"
#   .\scripts\weekly_expiry_alert_register.ps1 -Day Monday -At "06:30"
param(
    [string]$At = "18:00",
    [ValidateSet("Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday")]
    [string]$Day = "Sunday"
)

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\weekly_expiry_alert.ps1`""

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Day -At $At

# Runs only while the user is logged on (no stored password needed).
# StartWhenAvailable fires a missed run when the PC next wakes.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerOptionsExpiryAlert" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Weekly Telegram alert for options expiring within the next 7 days. Reads the portfolio sheet only (no broker sync); needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID configured." `
    -Force

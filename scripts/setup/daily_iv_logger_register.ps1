param([string]$At = "16:30") # near market close

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\daily\daily_iv_logger.ps1`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerIVLogger" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily ATM-IV snapshot logger for earnings playbook" `
    -Force

param([string]$At = "05:15") # after the US close (SGT)

$repo = "D:\Learn\Google\broker-portfolio-sync"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$repo\scripts\daily\daily_chain_snapshot.ps1`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "BrokerChainSnapshot" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily option-chain snapshots for backtests" `
    -Force

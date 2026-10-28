<#
.SYNOPSIS
  Stops and removes UniNotifier's scheduled tasks (the 15-minute check and the bot).

.DESCRIPTION
  Your settings (.env), state (data\) and downloaded course files are NOT touched:
  run scripts\setup_tasks.ps1 to bring the tasks back.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\remove_tasks.ps1
#>
[CmdletBinding()]
param(
    [string]$MonitorTaskName = 'UniNotifier',
    [string]$BotTaskName = 'UniNotifier_BotListener'
)
$ErrorActionPreference = 'Stop'

foreach ($name in $BotTaskName, $MonitorTaskName) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "[ OK ] Removed $name" -ForegroundColor Green
    } else {
        Write-Host "[ -- ] $name was not registered"
    }
}
Write-Host ''
Write-Host 'Nothing runs in the background any more. Your .env, data\ and course files are unchanged.'

<#
.SYNOPSIS
  Registers UniNotifier's two Windows scheduled tasks for the signed-in user.

.DESCRIPTION
  Finds this repository and its .venv by itself (nothing to edit), checks the setup with
  "python -m uninotifier.doctor", then registers - or replaces - two tasks:

    UniNotifier               the check: monitor.py every 15 minutes, forever. A run missed
                              while the PC was off or asleep runs right after it wakes; it
                              waits for a network connection; one run at a time; a run is
                              stopped after 15 minutes.
    UniNotifier_BotListener   the Telegram bot: bot.py at every sign-in, kept running with no
                              time limit, restarted every minute (up to 10 times) if it fails.

  Both run on battery too, without a console window (pythonw.exe), as you - no administrator
  rights needed. Running this again is safe: the tasks are replaced and the bot restarted
  with the current code. Remove them with scripts\remove_tasks.ps1.

.PARAMETER IntervalMinutes
  Minutes between checks (default 15; 5 to 60).

.PARAMETER SkipCheck
  Register even when the doctor finds a problem.

.PARAMETER NoStart
  Register only: don't start the bot now (it starts at your next sign-in).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1
#>
[CmdletBinding()]
param(
    [ValidateRange(5, 60)][int]$IntervalMinutes = 15,
    [switch]$SkipCheck,
    [switch]$NoStart,
    # advanced: other task names (the bot's self-heal restarts the default bot task name)
    [string]$MonitorTaskName = 'UniNotifier',
    [string]$BotTaskName = 'UniNotifier_BotListener'
)
$ErrorActionPreference = 'Stop'

function Fail([string]$message, [string]$fix) {
    Write-Host ''
    Write-Host "[FAIL] $message" -ForegroundColor Red
    if ($fix) { Write-Host "       -> $fix" -ForegroundColor Yellow }
    exit 1
}

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Monitor = Join-Path $Root 'monitor.py'
$Bot = Join-Path $Root 'bot.py'

Write-Host "UniNotifier - scheduled tasks setup"
Write-Host "Project: $Root"

if (-not (Test-Path $Pythonw)) {
    Fail "No virtual environment in $Root\.venv" `
         "double-click install.bat first (it creates .venv and installs everything)"
}
foreach ($file in $Monitor, $Bot) {
    if (-not (Test-Path $file)) { Fail "Missing $file" "run this script from a complete copy of the repository" }
}

if (-not $SkipCheck) {
    Write-Host ''
    Write-Host 'Checking the setup first (python -m uninotifier.doctor) ...'
    Write-Host ''
    Push-Location $Root
    try { & $Python -m uninotifier.doctor; $code = $LASTEXITCODE } finally { Pop-Location }
    if ($code -ne 0) {
        Fail 'The setup is not complete - no task was registered.' `
             'fix the [FAIL] lines above and run this again (or add -SkipCheck to register anyway)'
    }
}

# the signed-in user, as DOMAIN\name (works for local and Microsoft accounts)
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

# -- the check: every N minutes ------------------------------------------------------------
$monitorAction = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Monitor`"" -WorkingDirectory $Root
$monitorTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$monitorSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# -- the bot: at sign-in, always on --------------------------------------------------------
$botAction = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Bot`"" -WorkingDirectory $Root
$botTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$botSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -DontStopOnIdleEnd -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)

# a running bot keeps its old code: stop it before its task is replaced
if (Get-ScheduledTask -TaskName $BotTaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $BotTaskName -ErrorAction SilentlyContinue
}

Register-ScheduledTask -TaskName $MonitorTaskName -Action $monitorAction -Trigger $monitorTrigger `
    -Settings $monitorSettings -Principal $Principal -Force `
    -Description "UniNotifier: checks Moodle every $IntervalMinutes minutes and sends Telegram alerts ($Root)" | Out-Null
Register-ScheduledTask -TaskName $BotTaskName -Action $botAction -Trigger $botTrigger `
    -Settings $botSettings -Principal $Principal -Force `
    -Description "UniNotifier: the always-on Telegram bot ($Root)" | Out-Null

if (-not $NoStart) { Start-ScheduledTask -TaskName $BotTaskName }

Write-Host ''
foreach ($name in $MonitorTaskName, $BotTaskName) {
    $task = Get-ScheduledTask -TaskName $name
    $info = Get-ScheduledTaskInfo -TaskName $name
    $next = if ($info.NextRunTime) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { 'at sign-in' }
    Write-Host ("[ OK ] {0,-26} {1,-8} next run: {2}" -f $name, $task.State, $next) -ForegroundColor Green
}
Write-Host ''
Write-Host "The first check runs within a minute, then every $IntervalMinutes minutes."
Write-Host 'Logs: data\logs\monitor.log and data\logs\bot.log'
Write-Host 'Remove the tasks with:  powershell -ExecutionPolicy Bypass -File scripts\remove_tasks.ps1'

<#
  UniNotifier - survive Windows restarts.  Run ELEVATED (the UAC prompt is the consent).

  What it sets up (every change is recorded in data\windows_recovery.before.json, and
  "-Undo" puts all of it back):

    1. Windows Update never restarts the PC by itself while you are signed in
       (NoAutoRebootWithLoggedOnUsers), with "Configure Automatic Updates" set to the
       documented companion value (download, install at 03:00, you choose the restart).
    2. Automatic sign-in after any restart, so the logon-triggered bot and the scheduled
       checks come back within a minute. Only for an account with NO password (nothing
       secret is written to the registry); refuses otherwise.
    3. A task that locks the screen right after such an automatic sign-in (only when the
       PC booted less than 4 minutes ago), so the PIN still protects the desktop.

  Usage:   powershell -ExecutionPolicy Bypass -File scripts\windows_recovery.ps1 [-User name] [-Undo [-KeepUpdatePolicy]]
           (-Undo -KeepUpdatePolicy removes the automatic sign-in and the lock task and keeps item 1.)
#>
param(
    [string]$User = $env:USERNAME,
    [switch]$Undo,
    [switch]$KeepUpdatePolicy     # with -Undo: put back the sign-in settings and remove the lock task, but KEEP the update-restart policy
)
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root 'data'
$log    = Join-Path $dataDir 'windows_recovery.log'
$before = Join-Path $dataDir 'windows_recovery.before.json'
$taskName = 'UniNotifier_LockAfterAutoLogon'
$au  = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$wl  = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

function Say([string]$m) {
    $line = '{0} {1}' -f (Get-Date -Format 's'), $m
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding UTF8
}
function Current([string]$key, [string]$name) {
    $p = Get-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    return $p.$name
}
function SetValue([string]$key, [string]$name, $value, [string]$type) {
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name $name -Value $value -PropertyType $type -Force | Out-Null
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'Not elevated - nothing was changed.'
    exit 1
}

# the values this script owns: key, name, type
$owned = @(
    @{ Key = $au; Name = 'NoAutoRebootWithLoggedOnUsers'; Type = 'DWord'; Value = 1 },
    @{ Key = $au; Name = 'NoAutoUpdate';                  Type = 'DWord'; Value = 0 },
    @{ Key = $au; Name = 'AUOptions';                     Type = 'DWord'; Value = 4 },
    @{ Key = $au; Name = 'ScheduledInstallDay';           Type = 'DWord'; Value = 0 },
    @{ Key = $au; Name = 'ScheduledInstallTime';          Type = 'DWord'; Value = 3 },
    @{ Key = $wl; Name = 'AutoAdminLogon';                Type = 'String'; Value = '1' },
    @{ Key = $wl; Name = 'DefaultUserName';               Type = 'String'; Value = $User },
    @{ Key = $wl; Name = 'DefaultDomainName';             Type = 'String'; Value = $env:COMPUTERNAME },
    @{ Key = $wl; Name = 'DefaultPassword';               Type = 'String'; Value = '' }
)

if ($Undo) {
    if (-not (Test-Path $before)) { Say 'No record of earlier values (windows_recovery.before.json) - nothing to undo.'; exit 1 }
    $saved = Get-Content $before -Raw | ConvertFrom-Json
    foreach ($o in $owned) {
        if ($KeepUpdatePolicy -and $o.Key -eq $au) {
            Say ('kept      {0}\{1} = {2}' -f $o.Key, $o.Name, (Current $o.Key $o.Name))
            continue
        }
        $old = $saved.PSObject.Properties[($o.Key + '|' + $o.Name)]
        if ($null -eq $old -or $null -eq $old.Value) {
            Remove-ItemProperty -Path $o.Key -Name $o.Name -ErrorAction SilentlyContinue
            Say ('removed   {0}\{1}' -f $o.Key, $o.Name)
        } else {
            SetValue $o.Key $o.Name $old.Value $o.Type
            Say ('restored  {0}\{1} = {2}' -f $o.Key, $o.Name, $old.Value)
        }
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "removed task $taskName"
    Say 'UNDONE.'
    exit 0
}

# --- safety: automatic sign-in only for an account that has no password
$account = Get-LocalUser -Name $User -ErrorAction Stop
if ($account.PasswordRequired) {
    Say "Account '$User' HAS a password: refusing to store anything for automatic sign-in. Nothing was changed."
    exit 2
}

# --- remember what was there (once; a second run must not overwrite the original values)
if (-not (Test-Path $before)) {
    $record = [ordered]@{}
    foreach ($o in $owned) { $record[($o.Key + '|' + $o.Name)] = Current $o.Key $o.Name }
    $record | ConvertTo-Json | Set-Content -Path $before -Encoding UTF8
    Say "recorded the earlier values in $before"
}

foreach ($o in $owned) {
    SetValue $o.Key $o.Name $o.Value $o.Type
    Say ('set       {0}\{1} = "{2}"' -f $o.Key, $o.Name, $o.Value)
}

# --- lock the screen after an automatic sign-in (only right after boot)
$lockCmd = "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt 4) { rundll32.exe user32.dll,LockWorkStation }"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -Command "' + $lockCmd + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$trigger.Delay = 'PT25S'      # the bot (a plain logon trigger) starts first
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force `
    -Description 'UniNotifier: lock the screen after the automatic sign-in that follows a restart.' | Out-Null
Say "registered task $taskName"

# --- report
Say '--- now in force'
foreach ($o in $owned) { Say ('{0}\{1} = "{2}"' -f $o.Key, $o.Name, (Current $o.Key $o.Name)) }
Say ('passwordless-mode policy (DevicePasswordLessBuildVersion): {0}' -f (Current 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device' 'DevicePasswordLessBuildVersion'))
Say 'DONE. Restart the PC once to test; "-Undo" reverses everything.'
exit 0

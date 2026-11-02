<#
  UniNotifier - survive Windows restarts.  OPTIONAL, and it never runs by itself: nothing else in
  this project calls it. You run it, by hand, from an administrator PowerShell (the UAC prompt
  and the "type YES" question below are your consent).

  It only edits a few documented Windows settings (listed below, with their exact names), writes
  nothing secret, downloads nothing and uses no network. Every change is logged in
  data\windows_recovery.log, the earlier values are kept in data\windows_recovery.before.json,
  and -Undo puts them all back.

  By default it does ONE thing:
    Windows Update never restarts the PC by itself while you are signed in
    (NoAutoRebootWithLoggedOnUsers; "Configure Automatic Updates" is set to the documented
    companion value: download, install at 03:00, you choose the restart).

  Extra, only when you ask for it:
    -AutoSignIn        sign in automatically after a restart, so the bot and the scheduled
                       checks come back by themselves. ONLY for an account with NO password
                       (the script tests that and refuses otherwise; no password is ever
                       stored). It also turns off "only allow Windows Hello sign-in"
                       (DevicePasswordLessBuildVersion = 0), without which Windows ignores
                       automatic sign-in.
    -LockAfterSignIn   with -AutoSignIn: a small task locks the screen right after such an
                       automatic sign-in (only when the PC booted less than 4 minutes ago).

  Usage (from the project folder):
    powershell -ExecutionPolicy Bypass -File scripts\windows_recovery.ps1 -Preview
        shows what would change and changes nothing (no administrator needed)
    powershell -ExecutionPolicy Bypass -File scripts\windows_recovery.ps1 [-AutoSignIn [-LockAfterSignIn]] [-User name] [-Yes]
    powershell -ExecutionPolicy Bypass -File scripts\windows_recovery.ps1 -Undo [-KeepUpdatePolicy]
        (-Undo -KeepUpdatePolicy removes the sign-in changes and the lock task, keeps the update policy.)
#>
param(
    [string]$User = $env:USERNAME,
    [switch]$AutoSignIn,          # opt-in: automatic sign-in after a restart (no-password accounts only)
    [switch]$LockAfterSignIn,     # opt-in, with -AutoSignIn: lock the screen right after it
    [switch]$Preview,             # only say what would change
    [switch]$Yes,                 # do not ask "type YES"
    [switch]$Undo,
    [switch]$KeepUpdatePolicy     # with -Undo: keep the update-restart policy
)
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root 'data'
$log    = Join-Path $dataDir 'windows_recovery.log'
$before = Join-Path $dataDir 'windows_recovery.before.json'
$taskName = 'UniNotifier_LockAfterAutoLogon'
$au  = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$wl  = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$pl  = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device'

function Say([string]$m, [switch]$NoLog) {
    $line = '{0} {1}' -f (Get-Date -Format 's'), $m
    Write-Host $line
    if (-not $NoLog) {
        New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
        Add-Content -Path $log -Value $line -Encoding UTF8
    }
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
function Id($o) { return ($o.Key + '|' + $o.Name) }

# Does the account have a blank password? An interactive (console) sign-in with an empty password
# succeeds only for a blank one; a wrong password is error 1326. PasswordRequired = False is NOT
# the same thing: it only says a blank password would be allowed.
Add-Type -Namespace UniNotifier -Name Logon -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("advapi32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern bool LogonUser(string user, string domain, string password, int logonType, int provider, out System.IntPtr token);
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern bool CloseHandle(System.IntPtr handle);
'@
function Test-BlankPassword([string]$name) {
    $token = [IntPtr]::Zero
    if ([UniNotifier.Logon]::LogonUser($name, '.', '', 2, 0, [ref]$token)) {
        [void][UniNotifier.Logon]::CloseHandle($token)
        return 'blank'
    }
    $code = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    if ($code -eq 1327) { return 'blank' }          # "account restriction": Windows refuses a blank password here (it only allows it at the console)
    if ($code -eq 1326) { return 'has-password' }   # "logon failure": the empty password is wrong
    return "unknown ($code)"
}

# every value this script can own: group, key, name, type, value it sets
$all = @(
    @{ Group = 'update'; Key = $au; Name = 'NoAutoRebootWithLoggedOnUsers'; Type = 'DWord';  Value = 1 },
    @{ Group = 'update'; Key = $au; Name = 'NoAutoUpdate';                  Type = 'DWord';  Value = 0 },
    @{ Group = 'update'; Key = $au; Name = 'AUOptions';                     Type = 'DWord';  Value = 4 },
    @{ Group = 'update'; Key = $au; Name = 'ScheduledInstallDay';           Type = 'DWord';  Value = 0 },
    @{ Group = 'update'; Key = $au; Name = 'ScheduledInstallTime';          Type = 'DWord';  Value = 3 },
    @{ Group = 'signin'; Key = $wl; Name = 'AutoAdminLogon';                Type = 'String'; Value = '1' },
    @{ Group = 'signin'; Key = $wl; Name = 'DefaultUserName';               Type = 'String'; Value = $User },
    @{ Group = 'signin'; Key = $wl; Name = 'DefaultDomainName';             Type = 'String'; Value = $env:COMPUTERNAME },
    @{ Group = 'signin'; Key = $wl; Name = 'DefaultPassword';               Type = 'String'; Value = '' },
    @{ Group = 'signin'; Key = $pl; Name = 'DevicePasswordLessBuildVersion'; Type = 'DWord'; Value = 0 }
)
$owned = @($all | Where-Object { $_.Group -eq 'update' -or $AutoSignIn })

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ------------------------------------------------------------------ undo
if ($Undo) {
    if (-not $isAdmin) { Say 'Not elevated - nothing was changed.'; exit 1 }
    if (-not (Test-Path $before)) { Say 'No record of earlier values (windows_recovery.before.json) - nothing to undo.'; exit 1 }
    $saved = Get-Content $before -Raw | ConvertFrom-Json
    foreach ($o in $all) {
        if ($KeepUpdatePolicy -and $o.Group -eq 'update') {
            Say ('kept      {0}\{1} = {2}' -f $o.Key, $o.Name, (Current $o.Key $o.Name))
            continue
        }
        $old = $saved.PSObject.Properties[(Id $o)]
        if ($null -eq $old) {
            Say ('no record {0}\{1} - left as it is' -f $o.Key, $o.Name)
        } elseif ($null -eq $old.Value) {
            Remove-ItemProperty -Path $o.Key -Name $o.Name -ErrorAction SilentlyContinue
            Say ('removed   {0}\{1}' -f $o.Key, $o.Name)
        } else {
            SetValue $o.Key $o.Name $old.Value $o.Type
            Say ('restored  {0}\{1} = {2}' -f $o.Key, $o.Name, $old.Value)
        }
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "removed task $taskName (if it was there)"
    $record = [ordered]@{}
    foreach ($o in $all) { $record[(Id $o)] = Current $o.Key $o.Name }
    $record | ConvertTo-Json | Set-Content -Path $before -Encoding UTF8
    Say "recorded the earlier values in $before"
    Say 'UNDONE.'
    exit 0
}

# ------------------------------------------------------------------ what will happen
$mode = if ($Preview) { 'PREVIEW - nothing is changed' } else { 'APPLY' }
Say "UniNotifier windows_recovery: $mode" -NoLog:$Preview
Say 'It edits only these Windows settings, nothing else; it downloads nothing, stores no password and uses no network:' -NoLog:$Preview
foreach ($o in $owned) {
    Say ('  {0}\{1}: {2} -> "{3}"' -f $o.Key, $o.Name, (Current $o.Key $o.Name), $o.Value) -NoLog:$Preview
}
if ($AutoSignIn -and $LockAfterSignIn) {
    Say "  plus one scheduled task, $taskName (locks the screen after the automatic sign-in)" -NoLog:$Preview
}
if (-not $AutoSignIn) {
    Say 'Automatic sign-in is NOT part of this run (add -AutoSignIn if you want it).' -NoLog:$Preview
}
if ($AutoSignIn) {
    $blank = Test-BlankPassword $User
    $verdict = switch ($blank) { 'blank' { 'no password (automatic sign-in is possible)' }
                                 'has-password' { 'HAS a password' }
                                 default { "could not be checked, $blank" } }
    Say ("Account '{0}': {1}" -f $User, $verdict) -NoLog:$Preview
    if ($blank -ne 'blank') {
        Say 'Automatic sign-in only works for an account with no password, and this script never stores one. Nothing was changed.' -NoLog:$Preview
        exit 2
    }
}
if ($Preview) { Say 'Preview only: nothing was changed.' -NoLog; exit 0 }

if (-not $isAdmin) { Say 'Not elevated - nothing was changed.'; exit 1 }
if (-not $Yes) {
    $answer = Read-Host 'Type YES to apply these changes (anything else cancels)'
    if ($answer -cne 'YES') { Say 'Cancelled - nothing was changed.'; exit 3 }
}

# --- remember what was there (never overwrite a value that was already recorded)
$record = [ordered]@{}
if (Test-Path $before) {
    $savedBefore = Get-Content $before -Raw | ConvertFrom-Json
    foreach ($p in $savedBefore.PSObject.Properties) { $record[$p.Name] = $p.Value }
}
$added = $false
foreach ($o in $all) {
    if (-not $record.Contains((Id $o))) { $record[(Id $o)] = Current $o.Key $o.Name; $added = $true }
}
if ($added) {
    $record | ConvertTo-Json | Set-Content -Path $before -Encoding UTF8
    Say "recorded the earlier values in $before"
}

foreach ($o in $owned) {
    SetValue $o.Key $o.Name $o.Value $o.Type
    Say ('set       {0}\{1} = "{2}"' -f $o.Key, $o.Name, $o.Value)
}

# --- optional: lock the screen after an automatic sign-in (only right after boot)
if ($AutoSignIn -and $LockAfterSignIn) {
    $lockCmd = "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt 4) { rundll32.exe user32.dll,LockWorkStation }"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -Command "' + $lockCmd + '"')
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    $trigger.Delay = 'PT25S'      # the bot (a plain logon trigger) starts first
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Force `
        -Description 'UniNotifier: lock the screen after the automatic sign-in that follows a restart.' | Out-Null
    Say "registered task $taskName"
}

# --- report
Say '--- now in force'
foreach ($o in $owned) { Say ('{0}\{1} = "{2}"' -f $o.Key, $o.Name, (Current $o.Key $o.Name)) }
if ($AutoSignIn) {
    Say 'DONE. Restart the PC once to test; "-Undo" reverses everything.'
} else {
    Say 'DONE. "-Undo" reverses it.'
}
exit 0

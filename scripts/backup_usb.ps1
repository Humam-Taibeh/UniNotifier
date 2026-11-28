<#
Copies the whole project (settings, sessions, state, code) to a USB drive.

    backup_usb.bat                                   (double-click: Enter = the folder next to the project,
                                                      or type a USB drive letter)
    powershell -ExecutionPolicy Bypass -File scripts\backup_usb.ps1 -Drive G:

The copy lands in <drive>\UniNotifier-backup, or in UniNotifier-backup beside the project folder. Nothing on the drive is deleted, so running it
again only adds new files and updates changed ones. The .venv folder is skipped: it is rebuilt
by scripts\install.ps1. Your .env and data\ hold passwords and sessions - keep the drive safe.
A drive that is still locked (BitLocker, VeraCrypt) must be unlocked first.
#>
param(
    [string]$Drive
)

$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot

function Show-Drives {
    Write-Host ""
    Write-Host "Drives on this PC (the letters can change from one plug-in to the next):"
    Write-Host ""
    $rows = Get-Volume | Where-Object { $_.DriveLetter } | Sort-Object DriveLetter
    foreach ($v in $rows) {
        $name = if ($v.FileSystemLabel) { $v.FileSystemLabel } else { "(no name)" }
        $size = if ($v.Size) { "{0:N1} GB" -f ($v.Size / 1GB) } else { "locked or unreadable" }
        $mark = if ($project.StartsWith("$($v.DriveLetter):", "OrdinalIgnoreCase")) { "   <- this project is here" } else { "" }
        Write-Host ("  {0}:   {1,-22} {2,-12} {3}{4}" -f $v.DriveLetter, $name, $v.DriveType, $size, $mark)
    }
    Write-Host ""
}

# The default home of the backup: UniNotifier-backup next to the project folder.
$localTarget = Join-Path (Split-Path -Parent $project) "UniNotifier-backup"

if (-not $Drive) {
    Show-Drives
    Write-Host "Backup folder next to the project: $localTarget"
    $Drive = Read-Host "Press Enter to use it, or type the letter of a USB drive (for example G)"
}

if (-not $Drive) {
    $target = $localTarget
} else {
    $root = $Drive.Trim().TrimEnd("\")
    if ($root -match "^[A-Za-z]:?$") { $root = $root.Substring(0, 1) + ":\" }
    if (-not (Test-Path -LiteralPath $root)) {
        Write-Host "Drive $Drive was not found or cannot be opened."
        Write-Host "Plug it in, unlock it if it is encrypted, and check its letter in File Explorer."
        exit 1
    }
    $target = Join-Path $root "UniNotifier-backup"
}
Write-Host "Copying $project"
Write-Host "     to $target"

robocopy $project $target /E /XD ".venv" "__pycache__" ".pytest_cache" /R:1 /W:1 /NFL /NDL /NJH /NP
# robocopy: 0-7 mean success (some files copied or nothing to do); 8 and above mean a failure.
if ($LASTEXITCODE -ge 8) {
    Write-Host "Backup FAILED (robocopy exit code $LASTEXITCODE)."
    exit $LASTEXITCODE
}

Write-Host "Backup finished: $target"
exit 0

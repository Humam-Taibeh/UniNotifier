<#
.SYNOPSIS
  Installs UniNotifier on this PC: Python environment, packages, browser, and your settings files.

.DESCRIPTION
  What install.bat runs when you double-click it. Step by step:

    1. finds Python 3.11 or newer (and tells you how to get it when there is none)
    2. creates the project's private Python environment (.venv) - or reuses it
    3. installs the exact package versions from requirements.txt, then UniNotifier itself
    4. downloads the headless Chromium browser used to sign in to Moodle
    5. creates .env and schedule.json from the examples (never overwrites yours)
       and opens the new ones in Notepad for you to fill in
    6. runs the doctor, which checks everything and says what is left to do

  Safe to run again at any time: nothing you filled in is ever replaced.
  Next steps (sign in, first check, scheduled tasks) are printed at the end.

.PARAMETER Python
  Use this python.exe to create the environment instead of searching for one.

.PARAMETER NoEditor
  Don't open Notepad (for automated runs).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$NoEditor
)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$MinVersion = [version]'3.11'

function Step([string]$text) { Write-Host ''; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "[ OK ] $text" -ForegroundColor Green }
function Fail([string]$message, [string]$fix) {
    Write-Host ''
    Write-Host "[FAIL] $message" -ForegroundColor Red
    if ($fix) { Write-Host "       -> $fix" -ForegroundColor Yellow }
    exit 1
}
function Run([string]$what, [string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE)" 'check your internet connection and run the installer again' }
}

# A Python's version, or $null when the command is missing, broken, or the Microsoft Store placeholder.
function VersionOf([string]$exe, [string[]]$prefix) {
    try {
        $out = & $exe @prefix -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -match '^\d+\.\d+\.\d+$') { return [version]$out }
    } catch { }
    return $null
}

Write-Host 'UniNotifier installer'
Write-Host "Project: $Root"
Set-Location $Root

# -- 1. Python ---------------------------------------------------------------------------------
Step 'Looking for Python 3.11 or newer'
$base = $null
if ($Python) {
    $v = VersionOf $Python @()
    if (-not $v -or $v -lt $MinVersion) { Fail "$Python is not Python 3.11 or newer" }
    $base = @{ Exe = $Python; Prefix = @(); Version = $v }
} else {
    # the py launcher (python.org installs it) first, then whatever "python" is on PATH
    foreach ($candidate in @(@{ Exe = 'py'; Prefix = @('-3') }, @{ Exe = 'python'; Prefix = @() })) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $v = VersionOf $candidate.Exe $candidate.Prefix
        if ($v -and $v -ge $MinVersion) { $base = $candidate + @{ Version = $v }; break }
    }
}
if (-not $base) {
    Fail 'Python 3.11 or newer was not found' ('install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH") ' +
         'or run:  winget install -e --id Python.Python.3.13  - then run this installer again')
}
Ok "Python $($base.Version)"

# -- 2. the project's environment --------------------------------------------------------------
Step 'Preparing the private Python environment (.venv)'
if ((Test-Path $VenvPython) -and (VersionOf $VenvPython @())) {
    Ok '.venv already exists - reusing it'
} else {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }    # a broken or half-made one
    Run 'Creating .venv' $base.Exe ($base.Prefix + @('-m', 'venv', $Venv))
    Ok '.venv created'
}

# -- 3. packages -------------------------------------------------------------------------------
Step 'Installing the packages (a few minutes the first time)'
Run 'Updating pip' $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet', '--disable-pip-version-check')
Run 'Installing requirements.txt' $VenvPython @('-m', 'pip', 'install', '-r', 'requirements.txt', '--quiet', '--disable-pip-version-check')
Run 'Installing UniNotifier' $VenvPython @('-m', 'pip', 'install', '--no-deps', '-e', '.', '--quiet', '--disable-pip-version-check')
Ok 'Packages installed'

# -- 4. browser --------------------------------------------------------------------------------
Step 'Downloading the Chromium browser (about 150 MB the first time)'
Run 'Installing Chromium' $VenvPython @('-m', 'playwright', 'install', 'chromium')
Ok 'Chromium installed'

# -- 5. your settings --------------------------------------------------------------------------
Step 'Your settings files'
$created = @()
foreach ($pair in @(@('.env.example', '.env'), @('schedule.example.json', 'schedule.json'))) {
    $target = Join-Path $Root $pair[1]
    if (Test-Path $target) {
        Ok "$($pair[1]) already exists - kept as it is"
    } else {
        Copy-Item (Join-Path $Root $pair[0]) $target
        Ok "$($pair[1]) created from $($pair[0])"
        $created += $target
    }
}
if ($created -and -not $NoEditor) {
    # Windows 11's Notepad opens a file as a tab of a window that may already be open, and the
    # process started here can end at once - so wait for Enter, not for Notepad.
    foreach ($file in $created) {
        $name = Split-Path -Leaf $file
        Write-Host ''
        if ($name -eq '.env') {
            Write-Host 'Notepad now opens .env: fill in your Moodle login and your Telegram bot (README, steps 4 and 5).' -ForegroundColor Yellow
        } else {
            Write-Host 'Notepad now opens schedule.json: replace the example classes with your own week.' -ForegroundColor Yellow
        }
        Start-Process notepad.exe -ArgumentList "`"$file`""
        Read-Host "Save it (Ctrl+S), then press Enter here to continue" | Out-Null
    }
}

# -- 6. check ----------------------------------------------------------------------------------
Step 'Checking the setup (python -m uninotifier.doctor)'
Write-Host ''
& $VenvPython -m uninotifier.doctor
$healthy = $LASTEXITCODE -eq 0

Write-Host ''
Write-Host '==================================================================================='
if ($healthy) {
    Write-Host 'Installed. Three commands are left (README, step 6):' -ForegroundColor Green
} else {
    Write-Host 'Installed - fix the [FAIL] lines above (usually .env), then check again with:' -ForegroundColor Yellow
    Write-Host '  .venv\Scripts\python -m uninotifier.doctor'
    Write-Host ''
    Write-Host 'After that, three commands are left (README, step 6):'
}
Write-Host '  .venv\Scripts\python -m uninotifier.tools.login_refresh      sign in to Moodle once'
Write-Host '  .venv\Scripts\python monitor.py                              the first, quiet check'
Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1   run it automatically'
Write-Host '==================================================================================='
exit 0

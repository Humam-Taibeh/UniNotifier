<#
.SYNOPSIS
  Installs UniNotifier on this PC: Python environment, packages, browser, and your settings files.

.DESCRIPTION
  What install.bat runs when you double-click it. Step by step:

    1. finds Python 3.11 or newer (and tells you how to get it when there is none)
    2. creates the project's private Python environment (.venv) - or reuses it
    3. installs the exact package versions from requirements.txt, then UniNotifier itself
    4. downloads the headless Chromium browser used to sign in to Moodle
    5. asks you five short questions and writes your settings (.env) itself - no file
       to edit (python -m uninotifier.setup); settings already filled in are kept
    6. runs the doctor, which checks everything and says what is left to do
    7. offers to finish: sign in to Moodle, make the first check, and switch on the two
       scheduled tasks - or prints those three commands for later

  Your timetable needs no file: the bot reads it from the registration portal.
  Safe to run again at any time: nothing you filled in is ever replaced.

.PARAMETER Python
  Use this python.exe to create the environment instead of searching for one.

.PARAMETER NoEditor
  Ask nothing (for automated runs): .env is made from .env.example, to be filled in later.

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
if ($Root -match '\\OneDrive( - [^\\]+)?\\') {
    # a synced folder can make .venv's files cloud-only and locks data\ files while they upload
    Write-Host ''
    Write-Host '[WARN] This folder is inside OneDrive. UniNotifier works best outside it:' -ForegroundColor Yellow
    Write-Host "       -> move the whole folder to $HOME and run install.bat there" -ForegroundColor Yellow
}

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
    Fail 'Python 3.11 or newer was not found' ('install it from https://www.python.org/downloads/ (the Python install manager, ' +
         'or the classic installer with "Add python.exe to PATH" ticked) or run:  winget install -e --id Python.Python.3.13  - ' +
         'then run this installer again')
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
Step 'Your settings'
$EnvFile = Join-Path $Root '.env'
if ($NoEditor) {
    # an automated run: nothing is asked, the file is made from the example to be filled in later
    if (Test-Path $EnvFile) {
        Ok '.env already exists - kept as it is'
    } else {
        Copy-Item (Join-Path $Root '.env.example') $EnvFile
        Ok '.env created from .env.example'
    }
} else {
    # everything the program needs is already there? then nothing is asked again
    & $VenvPython -c "import sys; from uninotifier import envcheck; sys.exit(1 if envcheck.problems() else 0)"
    if ($LASTEXITCODE -eq 0) {
        Ok '.env is already filled in - kept as it is (change it with: .venv\Scripts\python -m uninotifier.setup)'
    } else {
        Write-Host ''
        & $VenvPython -m uninotifier.setup
        if ($LASTEXITCODE -ne 0) { Fail 'The setup was not finished' 'double-click install.bat again to go on - nothing is lost' }
    }
}

# -- 6. check ----------------------------------------------------------------------------------
Step 'Checking the setup (python -m uninotifier.doctor)'
Write-Host ''
& $VenvPython -m uninotifier.doctor
$healthy = $LASTEXITCODE -eq 0

# -- 7. finish: sign in, the first check, the scheduled tasks -----------------------------------
if ($healthy -and -not $NoEditor) {
    Write-Host ''
    $go = Read-Host 'Sign in to Moodle and switch UniNotifier on now? [Y/n]'
    if ($go -notmatch '^\s*[nN]') {
        Step 'Signing in to Moodle (a browser window opens - approve Microsoft on your phone if it asks)'
        & $VenvPython -m uninotifier.tools.login_refresh
        if ($LASTEXITCODE -ne 0) {
            Fail 'The Moodle sign-in did not finish' ('check your e-mail and password (.venv\Scripts\python -m uninotifier.setup), ' +
                 'then double-click install.bat again')
        }
        Step 'The first check: it records what is on Moodle now, quietly (a minute or two)'
        & $VenvPython (Join-Path $Root 'monitor.py')
        if ($LASTEXITCODE -ne 0) { Fail 'The first check did not finish' 'look at data\logs\monitor.log, then double-click install.bat again' }
        Step 'Switching it on (two Windows scheduled tasks - no administrator rights needed)'
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup_tasks.ps1') -SkipCheck
        if ($LASTEXITCODE -ne 0) { Fail 'The scheduled tasks could not be registered' 'run it by hand: powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1' }
        Write-Host ''
        Write-Host '===================================================================================' -ForegroundColor Green
        Write-Host 'UniNotifier is running. Open your bot in Telegram and send /start.' -ForegroundColor Green
        Write-Host 'Your timetable arrives by itself a few minutes from now, read from the portal.' -ForegroundColor Green
        Write-Host '===================================================================================' -ForegroundColor Green
        exit 0
    }
}

Write-Host ''
Write-Host '==================================================================================='
if ($healthy) {
    Write-Host 'Installed. Three commands are left (README, Installing by hand, e.):' -ForegroundColor Green
} else {
    Write-Host 'Installed - fix the [FAIL] lines above, then check again with:' -ForegroundColor Yellow
    Write-Host '  .venv\Scripts\python -m uninotifier.setup      answer the questions again'
    Write-Host '  .venv\Scripts\python -m uninotifier.doctor'
    Write-Host ''
    Write-Host 'After that, three commands are left (README, Installing by hand, e.):'
}
Write-Host '  (type them in PowerShell in this folder: in File Explorer, type powershell in the address bar)'
Write-Host '  .venv\Scripts\python -m uninotifier.tools.login_refresh      sign in to Moodle once'
Write-Host '  .venv\Scripts\python monitor.py                              the first, quiet check'
Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\setup_tasks.ps1   run it automatically'
Write-Host '==================================================================================='
exit 0

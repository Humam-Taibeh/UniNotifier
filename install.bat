@echo off
rem UniNotifier - double-click to install. Runs scripts\install.ps1 (safe to run again).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
pause

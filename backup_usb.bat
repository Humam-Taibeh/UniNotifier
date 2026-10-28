@echo off
rem UniNotifier - double-click to copy the whole project to a USB drive. Runs scripts\backup_usb.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\backup_usb.ps1" %*
echo.
pause

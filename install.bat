@echo off
rem UniNotifier - double-click to install. Runs scripts\install.ps1 (safe to run again).
cd /d "%~dp0"
if not exist "%~dp0scripts\install.ps1" goto :not_extracted
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
goto :done
:not_extracted
rem opened straight from the ZIP: Windows copied only this file to a temporary folder
echo [FAIL] install.bat can't find the rest of UniNotifier next to it.
echo        -^> if you downloaded the ZIP: right-click it, choose "Extract All...", then
echo           double-click install.bat inside the extracted folder.
:done
echo.
pause

@echo off
setlocal DisableDelayedExpansion
set "COLLECTOR_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "COLLECTOR_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%COLLECTOR_PS%" (
  echo Windows system PowerShell was not found. No collector was run.
  pause
  exit /b 2
)
"%COLLECTOR_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Verify-Collector.ps1"
set "COLLECTOR_EXIT=%ERRORLEVEL%"
echo.
echo Send this run's public JSON reports only. Keep collector-data and all private files on this PC.
pause
exit /b %COLLECTOR_EXIT%

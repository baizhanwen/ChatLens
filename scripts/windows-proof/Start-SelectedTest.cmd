@echo off
setlocal DisableDelayedExpansion
set "SELECTED_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "SELECTED_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%SELECTED_PS%" (
  echo Windows system PowerShell was not found. No test was run.
  pause
  exit /b 2
)
"%SELECTED_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Verify-SelectedTest.ps1"
set "SELECTED_EXIT=%ERRORLEVEL%"
echo.
echo Send only result-to-send.json. Keep all private files on this PC.
pause
exit /b %SELECTED_EXIT%

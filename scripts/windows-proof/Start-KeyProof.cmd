@echo off
setlocal DisableDelayedExpansion
set "KEYPROOF_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "KEYPROOF_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%KEYPROOF_PS%" (
  echo Windows system PowerShell was not found. No proof was run.
  pause
  exit /b 2
)
"%KEYPROOF_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Verify-Key.ps1"
set "KEYPROOF_EXIT=%ERRORLEVEL%"
echo.
echo Key prerequisite proof only. Do not send private folders or config.json.
pause
exit /b %KEYPROOF_EXIT%

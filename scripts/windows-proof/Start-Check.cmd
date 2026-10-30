@echo off
setlocal
set "CHECK_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "CHECK_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%CHECK_PS%" (
  echo Windows system PowerShell was not found. No check was run.
  pause
  exit /b 2
)
"%CHECK_PS%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0Check-Windows.ps1"
set "CHECK_EXIT=%ERRORLEVEL%"
echo.
echo Environment metadata only. Chat reading is NOT verified by this check.
pause
exit /b %CHECK_EXIT%

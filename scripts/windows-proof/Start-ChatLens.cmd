@echo off
setlocal DisableDelayedExpansion
echo ChatLens Windows Web r3 - keep this window open and follow the local browser guide.
set "CHATLENS_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "CHATLENS_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%CHATLENS_PS%" (
  echo Windows system PowerShell was not found. No local Web server was started.
  pause
  exit /b 2
)
"%CHATLENS_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Start-Web.ps1"
set "CHATLENS_EXIT=%ERRORLEVEL%"
echo.
echo Keep private data and keys on this PC. Send public diagnostic JSON only.
pause
exit /b %CHATLENS_EXIT%

@echo off
setlocal
set "READER_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "READER_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%READER_PS%" (
  echo Windows system PowerShell was not found. No check was run.
  pause
  exit /b 2
)
"%READER_PS%" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Check-ReaderSetup.ps1"
set "READER_EXIT=%ERRORLEVEL%"
echo.
echo Prerequisites only. Chat reading is NOT verified. Send only result-to-send.json.
pause
exit /b %READER_EXIT%

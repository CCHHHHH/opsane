@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo Opsane failed to start. Review the message above and the data\logs directory.
  pause
)
exit /b %errorlevel%

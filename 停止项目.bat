@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PS=pwsh"
where pwsh >nul 2>&1
if errorlevel 1 set "PS=powershell"

"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
pause
endlocal & exit /b %RC%

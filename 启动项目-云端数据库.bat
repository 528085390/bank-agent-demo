@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PS=pwsh"
where pwsh >nul 2>&1
if errorlevel 1 set "PS=powershell"

echo [云端数据库] 数据库入口：仓库根 database.txt（阿里云实例）
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Database cloud %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [启动失败] 退出码 %RC%。请查看上方输出，服务日志在 logs 目录。
) else (
    echo 服务已在后台运行，本窗口可以关闭。
)
echo.
pause
endlocal & exit /b %RC%

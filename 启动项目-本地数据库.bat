@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PS=pwsh"
where pwsh >nul 2>&1
if errorlevel 1 set "PS=powershell"

echo [本地数据库] 凭据取自仓库根 database.local.txt（地址和端口由 start.ps1 从该文件读取）
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Database local %*
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

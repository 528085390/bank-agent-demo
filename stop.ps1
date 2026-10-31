<#
.SYNOPSIS
    停止 银行 AI Agent 的三个服务（Agent 后端 :8000、模拟银行 :8001、前端 :5173）。

.DESCRIPTION
    按 logs/pids.json 的 PID、可执行路径和启动时间核验归属，再结束本工程进程树。
    仅检查记录中的实际端口，不按默认端口清理其他项目。

.PARAMETER Quiet
    少说话，只输出结果（供 start.ps1 内部调用）。

.PARAMETER PidsOnly
    只结束本次启动脚本记录过的 PID，绝不按端口误伤手工起的进程。
    start.ps1 启动失败回滚时用这个模式。

.PARAMETER Restart
    停止完成后立刻调用 start.ps1 重新启动。

.PARAMETER PortTimeoutSec
    等待端口释放的秒数，默认 15。

.EXAMPLE
    ./stop.ps1

.EXAMPLE
    ./stop.ps1 -Restart
#>
[CmdletBinding()]
param(
    [switch]$Quiet,
    [switch]$PidsOnly,
    [switch]$Restart,
    [int]$PortTimeoutSec = 15
)

$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$Root    = $PSScriptRoot
$LogDir  = Join-Path $Root 'logs'
$PidFile = Join-Path $LogDir 'pids.json'

function Write-Step { param([string]$Text) if (-not $Quiet) { Write-Host ''; Write-Host ('== ' + $Text) -ForegroundColor Cyan } }
function Write-Ok   { param([string]$Text) Write-Host ('  [OK]   ' + $Text) -ForegroundColor Green }
function Write-Note { param([string]$Text) if (-not $Quiet) { Write-Host ('  [..]   ' + $Text) -ForegroundColor DarkGray } }
function Write-Caution { param([string]$Text) Write-Host ('  [WARN] ' + $Text) -ForegroundColor Yellow }
function Write-Fail { param([string]$Text) Write-Host ('  [FAIL] ' + $Text) -ForegroundColor Red }

function Stop-Tree {
    param([int]$ProcessId)
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        $name = $process.ProcessName
        & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
        return $name
    } catch {
        return $null
    }
}

function Test-RecordedProcess {
    param($Entry, [string]$ExpectedExe)
    if (-not $Entry -or -not $Entry.pid -or -not $Entry.started) { return $false }
    try {
        $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction Stop
        if (-not $process.Path -or [IO.Path]::GetFullPath($process.Path) -ne [IO.Path]::GetFullPath($ExpectedExe)) { return $false }
        if ($Entry.started -is [DateTime]) {
            $started = $Entry.started.ToUniversalTime()
        } elseif ($Entry.started -is [DateTimeOffset]) {
            $started = $Entry.started.UtcDateTime
        } else {
            $started = [DateTimeOffset]::Parse([string]$Entry.started).UtcDateTime
        }
        return [Math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -lt 2
    } catch { return $false }
}

function Get-PortOwners {
    param([int]$Port)
    $owners = @()
    try {
        $connections = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop
        foreach ($connection in $connections) {
            $owners += [int]$connection.OwningProcess
        }
    } catch {
        $netstat = & netstat.exe -ano 2>$null
        foreach ($line in $netstat) {
            if ($line -match (':(' + $Port + ')\s') -and $line -match 'LISTENING') {
                $parts = ($line.Trim() -split '\s+')
                $owners += [int]$parts[-1]
            }
        }
    }
    return @($owners | Sort-Object -Unique)
}

$targets = @()

Write-Step '停止服务'

$stopped = 0
if (Test-Path $PidFile) {
    try {
        $data = Get-Content -Path $PidFile -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $data) {
            foreach ($property in $data.PSObject.Properties) {
                $entry = $property.Value
                if (-not $entry -or -not $entry.pid) { continue }
                $expectedExe = switch ($property.Name) {
                    'backend' { Join-Path $Root 'backend/.venv/Scripts/python.exe' }
                    'bank' { Join-Path $Root 'bank_backend/.venv/Scripts/python.exe' }
                    'frontend' { (Get-Command node.exe -ErrorAction SilentlyContinue).Source }
                    default { '' }
                }
                if (-not $expectedExe -or -not (Test-RecordedProcess $entry $expectedExe)) {
                    Write-Caution ($property.Name + ' 的进程记录已失效或归属不符，跳过停止。')
                    continue
                }
                if ($entry.port) {
                    $targets += [pscustomobject]@{ Name = $property.Name; Title = $property.Name; Port = [int]$entry.port }
                }
                $name = Stop-Tree -ProcessId ([int]$entry.pid)
                if ($name) {
                    Write-Ok ($property.Name + ' 已停止  PID ' + $entry.pid + '  (' + $name + ')')
                    $stopped = $stopped + 1
                }
            }
        }
    } catch {
        Write-Caution ('读取 ' + $PidFile + ' 失败，不按端口停止未知进程：' + $_.Exception.Message)
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}

if ($PidsOnly) {
    if ($stopped -eq 0) { Write-Note '没有记录中的进程需要停止' }
    Write-Host ''
    exit 0
}

# 未记录或身份不匹配的端口不属于本次停止范围。
# 等端口真的释放，避免刚 stop 就 start 撞在 TIME_WAIT 上。
$deadline = (Get-Date).AddSeconds($PortTimeoutSec)
$stillBusy = @()
while ((Get-Date) -lt $deadline) {
    $stillBusy = @($targets | Where-Object { @(Get-PortOwners -Port $_.Port).Count -gt 0 })
    if ($stillBusy.Count -eq 0) { break }
    Start-Sleep -Milliseconds 400
}

if ($stillBusy.Count -eq 0) {
    Write-Ok '本工程记录的服务已停止，其他项目未停止'
} else {
    foreach ($target in $stillBusy) {
        Write-Fail ($target.Title + ' :' + $target.Port + ' 仍在监听，请手动检查')
    }
}

Write-Host ''
if ($stopped -eq 0) { Write-Note '没有发现需要停止的服务' }

if ($Restart) {
    Write-Step '重新启动'
    & (Join-Path $Root 'start.ps1')
}

exit 0
